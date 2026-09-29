"""Командная строка uwb-fw-update."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from . import DEFAULT_CONFIG, DEFAULT_HOME, __version__
from . import daemon, report as reportmod, runner, selfupdate
from .cache import Cache, CacheError
from .config import load_config, select_devices
from .fsutil import read_json
from .progress import FINAL_STATUSES, read_state
from .releases import KIND_BOOT, KIND_FW, Releases
from .runner import EXIT_ENV, EXIT_FAILED, EXIT_OK, EXIT_USAGE, RunOptions
from .state_db import StateDb
from .watch import watch as watch_run


def _add_filters(p):
    g = p.add_argument_group("выбор устройств")
    g.add_argument("--port", dest="ports", action="append", default=[],
                   help="шина: /dev/ttyRS485-1, 192.168.1.24:23 или адрес шлюза (все его порты); повторяемо")
    g.add_argument("--slave", dest="slaves", action="append", type=int, default=[],
                   help="адрес устройства; повторяемо")
    g.add_argument("--device-id", dest="device_ids", action="append", default=[],
                   help="id или name устройства из конфигурации; повторяемо")


def _add_run_opts(p, mode):
    _add_filters(p)
    g = p.add_argument_group("выполнение")
    g.add_argument("--jobs", type=int, default=4, help="сколько шин обрабатывать параллельно (по умолчанию 4)")
    g.add_argument("--suite", help="канал релизов (по умолчанию SUITE из /usr/lib/wb-release)")
    g.add_argument("--offline", action="store_true", help="только локальный кэш, без сети")
    g.add_argument("--access", choices=runner.ACCESS_MODES, default=runner.ACCESS_DIRECT,
                   help="доступ к шинам: direct — напрямую, wb-mqtt-serial на время прогона останавливается "
                        "(по умолчанию); rpc — через RPC port/Load драйвера, он продолжает работать; "
                        "auto — rpc, если драйвер запущен и RPC доступен, иначе direct")
    g.add_argument("--mqtt", default="localhost:1883", metavar="HOST[:PORT]",
                   help="MQTT-брокер для режима rpc (по умолчанию localhost:1883)")
    g.add_argument("--no-stop-serial", action="store_true",
                   help="direct: не останавливать wb-mqtt-serial (если вы остановили его сами)")
    g.add_argument("--trace", action="store_true", help="hex-кадры Modbus в лог шины")
    g.add_argument("--recover-attempts", type=int, default=2)
    g.add_argument("--pause-timeout", type=int, default=30, metavar="MIN",
                   help="пауза дольше MIN минут превращается в мягкую остановку (0 — без ограничения; "
                        "на паузе wb-mqtt-serial остановлен; в режиме rpc не действует)")
    g.add_argument("--foreground", action="store_true",
                   help="выполнять в текущей консоли (закрытие консоли прервёт прогон)")
    g.add_argument("--detach", action="store_true", help="запустить в фоне и сразу выйти")
    g.add_argument("-y", "--yes", action="store_true", help="не задавать вопросов")
    if mode == "update":
        g.add_argument("--force", action="store_true", help="перезаписать прошивку той же версии")
        g.add_argument("--allow-downgrade", action="store_true", help="разрешить откат на версию релиза")
        g.add_argument("--no-bootloader", "--fw-only", dest="no_bootloader", action="store_true",
                       help="не обновлять загрузчики")
        g.add_argument("--force-bootloader", action="store_true",
                       help="перезаписать загрузчик, даже если версия совпадает с релизной (осторожно!)")
        g.add_argument("--resume", metavar="RUN_ID",
                       help="продолжить остановленный/прерванный прогон: только недообработанные устройства")
    if mode == "recover":
        g.add_argument("--from-run", metavar="RUN_ID", help="восстановить прерванные устройства прогона")


def build_parser():
    ap = argparse.ArgumentParser(prog="uwb-fw-update",
                                 description="Безопасное массовое обновление прошивок устройств Wiren Board")
    ap.add_argument("--home", default=os.environ.get("UWBFWUP_HOME", DEFAULT_HOME),
                    help=f"каталог кэша, прогонов и базы устройств (по умолчанию {DEFAULT_HOME})")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="конфигурация wb-mqtt-serial")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="что будет обработано и что пропущено (по конфигурации)")
    _add_filters(p)
    p.add_argument("--all", action="store_true", help="показать и пропущенные")
    p.add_argument("--json", action="store_true")

    for mode, text in (("check", "опросить устройства и сравнить версии, ничего не прошивая"),
                       ("update", "обновить загрузчики и прошивки"),
                       ("recover", "найти устройства в загрузчике и восстановить прошивку")):
        _add_run_opts(sub.add_parser(mode, help=text), mode)

    p = sub.add_parser("watch", help="живой просмотр текущего/указанного прогона")
    p.add_argument("--run")
    p.add_argument("--from-start", action="store_true", help="показать все события с начала")

    p = sub.add_parser("status", help="снимок состояния прогона")
    p.add_argument("--run")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("report", help="отчёт прогона")
    p.add_argument("--run")
    p.add_argument("--failed-only", action="store_true")
    p.add_argument("--all", action="store_true", help="показать и пропущенные по конфигурации")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("logs", help="лог шины или прогона")
    p.add_argument("--run")
    p.add_argument("--bus", help="ключ шины; без него — общий лог прогона")
    p.add_argument("-f", "--follow", action="store_true")

    p = sub.add_parser("stop", help="остановить текущий прогон")
    p.add_argument("--now", action="store_true", help="прервать запись прошивок (загрузчик дописывается)")
    p.add_argument("--bus", help="остановить только одну шину")
    p.add_argument("-y", "--yes", action="store_true")
    sub.add_parser("pause", help="пауза перед следующими устройствами")
    sub.add_parser("resume", help="снять паузу")

    p = sub.add_parser("runs", help="список прогонов")
    p = sub.add_parser("prune-runs", help="удалить старые прогоны")
    p.add_argument("--keep", type=int, default=20)

    p = sub.add_parser("cache", help="локальный кэш прошивок")
    csub = p.add_subparsers(dest="cache_cmd", required=True)
    c = csub.add_parser("sync", help="скачать прошивки/загрузчики для известных устройств")
    _add_filters(c)
    c.add_argument("--suite")
    c.add_argument("--signature", action="append", default=[], help="дополнительно для сигнатуры")
    csub.add_parser("list")
    csub.add_parser("verify")
    c = csub.add_parser("prune", help="удалить файлы, не нужные текущему релизу известных устройств")
    c.add_argument("--suite")
    c.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("version", help="версия утилиты; --check — есть ли новая на GitHub")
    p.add_argument("--check", action="store_true", help="узнать последнюю версию на GitHub")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("self-update", help="обновить саму утилиту до последнего релиза с GitHub")
    p.add_argument("--check-only", action="store_true", help="только проверить, не обновлять")
    p.add_argument("--force", action="store_true", help="переустановить, даже если версия не новее")
    p.add_argument("-y", "--yes", action="store_true", help="не задавать вопросов")

    p = sub.add_parser("_worker")
    p.add_argument("run_dir")
    p = sub.add_parser("_poststop")
    p.add_argument("run_dir")
    return ap


# --- команды ------------------------------------------------------------

def cmd_list(args):
    model = load_config(args.config)
    devices = select_devices(model, args.ports, args.slaves, args.device_ids)
    if args.json:
        print(json.dumps([{**d.__dict__, "params": str(d.params)} for d in devices], ensure_ascii=False, indent=1))
        return EXIT_OK
    by_bus = {}
    for d in devices:
        by_bus.setdefault(d.bus_key, []).append(d)
    active = 0
    for bus in model.buses:
        devs = by_bus.get(bus.key)
        if not devs:
            continue
        ok = [d for d in devs if not d.skip_reason]
        active += len(ok)
        state = "" if bus.enabled else " [ОТКЛЮЧЁН]"
        print(f"== {bus.key} ({bus.kind}){state}: к обработке {len(ok)}, пропуск {len(devs) - len(ok)}")
        for d in devs:
            if d.skip_reason and not args.all:
                continue
            mark = f"  пропуск: {d.skip_reason}" if d.skip_reason else ""
            print(f"   {d.raw_slave:>6}  {d.params!s:<9} {d.device_type:<24} {d.label}{mark}")
    for w in model.warnings:
        print(f"предупреждение: {w}")
    print(f"\nвсего к обработке: {active}")
    return EXIT_OK


def _resolve_run(args):
    run_id = getattr(args, "run", None) or daemon.last_run_id(args.home)
    if not run_id:
        print("прогонов нет", file=sys.stderr)
        return None, None
    run_dir = daemon.run_dir_of(args.home, run_id)
    if not os.path.isdir(run_dir):
        print(f"прогон {run_id} не найден", file=sys.stderr)
        return None, None
    return run_id, run_dir


def _check_active_and_orphans(args):
    """-> (ok, прерванные устройства аварийного прогона)."""
    cur = daemon.current_run(args.home)
    if cur:
        run_id, run_dir, state = cur
        if daemon.is_alive(run_id, state):
            print(f"уже выполняется прогон {run_id}. Просмотр: uwb-fw-update watch; остановка: uwb-fw-update stop",
                  file=sys.stderr)
            return False, []
        devices = daemon.mark_crashed(args.home, run_id, run_dir, state)
    else:
        crashed = daemon.crashed_run(args.home)
        if not crashed:
            return True, []
        run_id, run_dir, state = crashed
        devices = [tuple(x) for x in state.get("crashed_devices", [])]
        daemon.acknowledge_crash(args.home, run_id)
    print(f"ВНИМАНИЕ: прогон {run_id} завершился аварийно (процесс отсутствует).", file=sys.stderr)
    if devices:
        print("  устройства, обрабатывавшиеся в момент сбоя (могут быть в загрузчике): "
              + ", ".join(f"{b} #{s}" for b, s in devices), file=sys.stderr)
        print("  они будут добавлены в текущий прогон для проверки и восстановления.", file=sys.stderr)
    return True, devices


def cmd_run(args):
    if not os.path.exists(args.config):
        print(f"нет файла конфигурации {args.config}", file=sys.stderr)
        return EXIT_ENV
    ok, crashed = _check_active_and_orphans(args)
    if not ok:
        return EXIT_ENV
    if runner.lock_busy(RunOptions(home=os.path.abspath(args.home))):
        print("уже выполняется другой прогон (занята блокировка). Просмотр: uwb-fw-update watch", file=sys.stderr)
        return EXIT_ENV
    opts = RunOptions(
        mode=args.cmd, config=os.path.abspath(args.config), home=os.path.abspath(args.home),
        ports=args.ports, slaves=args.slaves, device_ids=args.device_ids,
        force=getattr(args, "force", False), allow_downgrade=getattr(args, "allow_downgrade", False),
        no_bootloader=getattr(args, "no_bootloader", False),
        force_bootloader=getattr(args, "force_bootloader", False), recover_attempts=args.recover_attempts,
        jobs=args.jobs, suite=args.suite, offline=args.offline, stop_serial=not args.no_stop_serial,
        access=args.access, mqtt=args.mqtt,
        trace=args.trace, foreground=args.foreground, pause_timeout_min=args.pause_timeout,
    )
    source_run = getattr(args, "resume", None) or getattr(args, "from_run", None)
    if source_run:
        src_dir = daemon.run_dir_of(args.home, source_run)
        if not os.path.isdir(src_dir):
            print(f"прогон {source_run} не найден", file=sys.stderr)
            return EXIT_USAGE
        todo = runner.resumable_devices(src_dir)
        if not todo:
            print(f"в прогоне {source_run} нет недообработанных устройств")
            return EXIT_OK
        opts.include, opts.include_only = [list(x) for x in todo], True
        print(f"продолжение прогона {source_run}: устройств {len(todo)}")
    if crashed:
        opts.include += [list(x) for x in crashed if list(x) not in opts.include]

    if not opts.offline:
        notice = selfupdate.update_notice(opts.home)
        if notice:
            print(notice, file=sys.stderr)

    # быстрая проверка до запуска: конфиг разбирается, фильтры что-то выбирают
    model = load_config(opts.config)
    targets, skipped = runner.plan(opts, model)
    total = sum(len(v) for v in targets.values())
    print(f"к обработке: {total} устройств на {len(targets)} шинах; пропуск по конфигурации: {len(skipped)}")
    if not total:
        return EXIT_OK
    if opts.mode == "update" and not args.yes and sys.stdin.isatty() and not args.detach:
        answer = input("Начать обновление? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "д", "да"):
            return EXIT_USAGE

    run_id, run_dir = runner.create_run(opts)
    if opts.foreground:
        return runner.Runner(run_id, run_dir, opts).execute()
    how = daemon.launch(opts.home, run_id, run_dir)
    print(f"прогон {run_id} запущен в фоне ({how}).")
    print("  просмотр: uwb-fw-update watch   остановка: uwb-fw-update stop   отчёт: uwb-fw-update report")
    if args.detach:
        return EXIT_OK
    # ждём, пока воркер создаст состояние
    for _ in range(50):
        if read_state(run_dir):
            break
        time.sleep(0.1)
    return watch_run(run_dir, from_start=True, send_control=lambda a: daemon.send_control(run_dir, a))


def cmd_watch(args):
    run_id, run_dir = _resolve_run(args)
    if not run_dir:
        return EXIT_USAGE
    return watch_run(run_dir, from_start=args.from_start, send_control=lambda a: daemon.send_control(run_dir, a))


def cmd_status(args):
    run_id, run_dir = _resolve_run(args)
    if not run_dir:
        return EXIT_USAGE
    state = read_state(run_dir)
    if state.get("status") not in FINAL_STATUSES and state.get("pid") and not daemon.is_alive(run_id, state):
        state["status"] = f"{state.get('status')} (процесс не найден — аварийное завершение?)"
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=1))
    else:
        from .watch import render_state
        from .progress import iter_events
        events = list(iter_events(os.path.join(run_dir, "events.jsonl")))
        print("\n".join(render_state(state, events)))
    return EXIT_OK


def cmd_report(args):
    run_id, run_dir = _resolve_run(args)
    if not run_dir:
        return EXIT_USAGE
    rep = read_json(os.path.join(run_dir, "report.json"))
    if not rep:
        state = read_state(run_dir)
        print(f"отчёта нет: прогон {run_id} в состоянии {state.get('status')}", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(reportmod.render_text(rep, failed_only=args.failed_only, show_skipped=args.all))
    return EXIT_OK if rep.get("ok") else EXIT_FAILED


def cmd_logs(args):
    run_id, run_dir = _resolve_run(args)
    if not run_dir:
        return EXIT_USAGE
    path = runner.bus_log_path(run_dir, args.bus) if args.bus else os.path.join(run_dir, "run.log")
    if not os.path.exists(path):
        print(f"нет лога {path}", file=sys.stderr)
        return EXIT_USAGE
    if args.follow and shutil.which("tail"):
        return subprocess.call(["tail", "-n", "100", "-F", path])
    with open(path, encoding="utf-8", errors="replace") as fp:
        sys.stdout.write(fp.read())
    return EXIT_OK


def _active_run_dir(args):
    cur = daemon.current_run(args.home)
    if not cur or not daemon.is_alive(cur[0], cur[2]):
        print("нет активного прогона", file=sys.stderr)
        return None, None
    return cur[1], cur[2]


def cmd_stop(args):
    run_dir, state = _active_run_dir(args)
    if not run_dir:
        return EXIT_USAGE
    if args.now:
        bl_buses = [k for k, st in (state.get("buses") or {}).items()
                    if st.get("stage") == "flash_bl" and (not args.bus or k == args.bus)]
        if bl_buses:
            print("на шинах идёт запись загрузчика: " + ", ".join(bl_buses))
            print("она не будет прервана — шина остановится после записи загрузчика и прошивки.")
            if not args.yes and sys.stdin.isatty():
                if input("Продолжить? [y/N] ").strip().lower() not in ("y", "yes"):
                    return EXIT_USAGE
    daemon.send_control(run_dir, "stop_now" if args.now else "stop", args.bus)
    what = "быстрая" if args.now else "мягкая"
    print(f"{what} остановка {'шины ' + args.bus if args.bus else 'прогона'} запрошена; "
          "ход: uwb-fw-update watch")
    return EXIT_OK


def cmd_pause(args, action):
    run_dir, _ = _active_run_dir(args)
    if not run_dir:
        return EXIT_USAGE
    daemon.send_control(run_dir, action)
    print("пауза запрошена (текущие устройства будут доведены до конца)" if action == "pause"
          else "продолжение запрошено")
    return EXIT_OK


def cmd_runs(args):
    for run_id in daemon.list_runs(args.home):
        state = read_state(daemon.run_dir_of(args.home, run_id))
        rep = read_json(os.path.join(daemon.run_dir_of(args.home, run_id), "report.json")) or {}
        print(f"{run_id}  {state.get('mode', ''):<8} {state.get('status', ''):<10} "
              f"{reportmod.summary_line(rep) if rep else ''}")
    return EXIT_OK


def cmd_prune_runs(args):
    runs = daemon.list_runs(args.home)
    cur = daemon.current_run(args.home)
    keep = set(runs[-args.keep:]) if args.keep > 0 else set()
    if cur:
        keep.add(cur[0])
    for run_id in runs:
        if run_id not in keep:
            shutil.rmtree(daemon.run_dir_of(args.home, run_id), ignore_errors=True)
            print(f"удалён {run_id}")
    return EXIT_OK


def _known_signatures(args):
    db = StateDb(runner.devices_db(args.home))
    sigs = set(getattr(args, "signature", []) or [])
    if getattr(args, "ports", None) is not None and os.path.exists(args.config):
        model = load_config(args.config)
        for d in select_devices(model, args.ports, args.slaves, args.device_ids):
            if not d.skip_reason:
                sig = db.get(d.bus_key, d.slave).get("signature")
                if sig:
                    sigs.add(sig)
    else:
        sigs.update(db.signatures())
    return sorted(sigs)


def cmd_cache(args):
    cache = Cache(runner.cache_dir(args.home))
    if args.cache_cmd == "list":
        for rel in cache.list_files():
            print(f"{'ok ' if cache.verify(rel) else 'BAD'} {rel}")
        return EXIT_OK
    if args.cache_cmd == "verify":
        bad = [rel for rel in cache.list_files() if not cache.verify(rel)]
        for rel in bad:
            print(f"повреждён: {rel}")
        print(f"проверено файлов: {len(cache.list_files())}, повреждено: {len(bad)}")
        return EXIT_FAILED if bad else EXIT_OK
    releases = Releases(cache, args.suite)
    try:
        releases.load()
    except CacheError as e:
        print(f"индексы релизов недоступны: {e}", file=sys.stderr)
        return EXIT_ENV
    sigs = _known_signatures(args)
    if not sigs:
        print("сигнатуры устройств неизвестны: сначала выполните uwb-fw-update check")
        return EXIT_OK
    wanted = set()
    for sig in sigs:
        for kind in (KIND_FW, KIND_BOOT):
            rel = releases.resolve(kind, sig)
            if rel:
                wanted.add(rel.relpath)
    if args.cache_cmd == "sync":
        errors = 0
        for rel in sorted(wanted):
            try:
                cache.get_file(rel)
                print(f"ok  {rel}")
            except CacheError as e:
                errors += 1
                print(f"ERR {rel}: {e}")
        print(f"релиз {releases.suite}: сигнатур {len(sigs)}, файлов {len(wanted)}, ошибок {errors}")
        return EXIT_FAILED if errors else EXIT_OK
    if args.cache_cmd == "prune":
        for rel in cache.list_files():
            if rel not in wanted:
                print(f"{'будет удалён' if args.dry_run else 'удалён'}: {rel}")
                if not args.dry_run:
                    cache.remove(rel)
        return EXIT_OK
    return EXIT_USAGE


def cmd_version(args):
    if not args.check:
        print(__version__)
        return EXIT_OK
    try:
        rel = selfupdate.latest_release()
    except selfupdate.SelfUpdateError as e:
        print(f"не удалось узнать последнюю версию: {e}", file=sys.stderr)
        return EXIT_ENV
    newer = selfupdate.is_newer(rel["version"])
    if args.json:
        print(json.dumps({"version": __version__, "latest": rel["version"], "update_available": newer,
                          "url": rel["url"]}, ensure_ascii=False, indent=1))
    else:
        print(f"установлена: {__version__}")
        print(f"последняя:   {rel['version']}  {rel['url'] or ''}")
        print("доступно обновление: uwb-fw-update self-update" if newer else "обновление не требуется")
    return EXIT_OK


def cmd_self_update(args):
    try:
        rel = selfupdate.latest_release()
        newer = selfupdate.is_newer(rel["version"])
        print(f"установлена: {__version__}, последняя: {rel['version']}")
        if not newer and not args.force:
            print("обновление не требуется")
            return EXIT_OK
        if args.check_only:
            return EXIT_OK
        target = selfupdate.self_path()
        cur = daemon.current_run(args.home)
        if cur and daemon.is_alive(cur[0], cur[2]):
            print(f"идёт прогон {cur[0]} — обновите утилиту после его завершения", file=sys.stderr)
            return EXIT_ENV
        if not args.yes and sys.stdin.isatty():
            answer = input(f"Заменить {target} версией {rel['version']}? [y/N] ").strip().lower()
            if answer not in ("y", "yes", "д", "да"):
                return EXIT_USAGE
        selfupdate.install(rel, target)
    except selfupdate.SelfUpdateError as e:
        print(f"self-update: {e}", file=sys.stderr)
        return EXIT_ENV
    print(f"обновлено до {rel['version']}: {target} (прежняя версия: {target}.prev)")
    return EXIT_OK


# команды, которые останавливают wb-mqtt-serial, работают с портами, пишут в каталог данных
# или управляют воркером, запущенным от root
ROOT_COMMANDS = {"check", "update", "recover", "stop", "pause", "resume", "prune-runs", "self-update",
                 "_worker", "_poststop"}
ROOT_CACHE_COMMANDS = {"sync", "prune"}


def needs_root(args) -> bool:
    return args.cmd in ROOT_COMMANDS or (args.cmd == "cache" and args.cache_cmd in ROOT_CACHE_COMMANDS)


def is_root() -> bool:
    return not hasattr(os, "geteuid") or os.geteuid() == 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    if needs_root(args) and not is_root():
        what = f"cache {args.cache_cmd}" if args.cmd == "cache" else args.cmd
        if args.cmd in ("check", "update", "recover", "_worker", "_poststop"):
            why = "команда останавливает wb-mqtt-serial и работает с портами RS-485 и шлюзами"
        elif args.cmd in ("stop", "pause", "resume"):
            why = "команда управляет прогоном, запущенным от root"
        elif args.cmd == "self-update":
            why = "команда заменяет файл утилиты"
        else:
            why = f"команда пишет в {args.home}"
        print(f"uwb-fw-update {what}: нужны права root — {why}.\n"
              f"Запустите от root (например: sudo {sys.argv[0]} ...). Без root доступны просмотр: "
              f"list, status, watch, report, logs, runs, cache list/verify, version.", file=sys.stderr)
        return EXIT_ENV
    try:
        if args.cmd == "list":
            return cmd_list(args)
        if args.cmd in ("check", "update", "recover"):
            return cmd_run(args)
        if args.cmd == "watch":
            return cmd_watch(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "report":
            return cmd_report(args)
        if args.cmd == "logs":
            return cmd_logs(args)
        if args.cmd == "stop":
            return cmd_stop(args)
        if args.cmd in ("pause", "resume"):
            return cmd_pause(args, args.cmd)
        if args.cmd == "runs":
            return cmd_runs(args)
        if args.cmd == "prune-runs":
            return cmd_prune_runs(args)
        if args.cmd == "cache":
            return cmd_cache(args)
        if args.cmd == "version":
            return cmd_version(args)
        if args.cmd == "self-update":
            return cmd_self_update(args)
        if args.cmd == "_worker":
            run_dir = os.path.abspath(args.run_dir)
            return runner.Runner(os.path.basename(run_dir), run_dir, runner.load_options(run_dir)).execute()
        if args.cmd == "_poststop":
            return daemon.poststop(os.path.abspath(args.run_dir))
    except KeyboardInterrupt:
        return EXIT_USAGE
    except (OSError, ValueError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return EXIT_ENV
    return EXIT_USAGE
