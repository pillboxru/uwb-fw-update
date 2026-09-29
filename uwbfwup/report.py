"""Итоговый отчёт прогона: что обновилось, что нет, где и почему."""

import time
from collections import Counter, OrderedDict

REASON_HINTS = {
    "no_response": "устройство не отвечает: проверьте питание, адрес, скорость и линию",
    "tcp_requires_131_or_9600": "через шлюз нужен загрузчик >= 1.3.0 и прошивка с рег. 131 "
                                "или устройство и шлюз на 9600 8N2",
    "signature_mismatch": "прошивка не подходит устройству",
    "download_error": "не удалось скачать или проверить файл прошивки",
    "unknown_signature": "нужна сигнатура: uwb-fw-update recover ... после ручного указания или "
                         "wb-mcu-fw-updater recover --fw-sig",
    "bootloader_lost": "устройство не отвечает после обновления загрузчика — подключите напрямую "
                       "и выполните recover",
    "connection_error": "потеряно соединение со шлюзом",
    "unstable_link": "устройство отвечает с перебоями: проверьте линию, терминаторы, таймауты шлюза",
    "jump_failed": "устройство не перешло в загрузчик",
    "version_mismatch": "после записи версия не совпала с ожидаемой",
    "app_not_started": "прошивка записана, но устройство не запускается — повторите recover или "
                       "восстановите вручную при прямом подключении",
    "stuck_in_bootloader": "устройство осталось в загрузчике",
    "port_disabled": "порт отключён в конфигурации",
    "device_disabled": "устройство отключено в конфигурации",
    "wbio_module": "модуль WBIO прошивается вместе с WB-MIO",
    "foreign": "не WB-устройство или слишком старая прошивка",
    "no_release": "в релизе нет прошивки для этой сигнатуры",
    "cancelled": "прогон остановлен оператором",
    "signal": "прогон остановлен сигналом (перезагрузка контроллера или systemctl stop) — устройство, "
              "вероятно, в загрузчике; используйте --resume",
    "operator": "прервано оператором — устройство, вероятно, в загрузчике; используйте --resume",
}


def build(run_id, mode, results, meta=None, status="finished"):
    results = [r if isinstance(r, dict) else r.to_dict() for r in results]
    counts = Counter(r["status"] for r in results)
    return {
        "run_id": run_id,
        "mode": mode,
        "status": status,
        "generated_at": time.time(),
        "meta": meta or {},
        "counts": dict(counts),
        "ok": all(r["ok"] for r in results),
        "devices": results,
    }


def summary_line(report):
    c = report.get("counts", {})
    order = ["updated", "recovered", "up_to_date", "update_available", "alive", "in_bootloader",
             "skipped", "failed", "stuck_in_bootloader", "interrupted", "cancelled"]
    parts = [f"{name}: {c[name]}" for name in order if c.get(name)]
    return ", ".join(parts) or "устройств нет"


def _version_change(before, after, target):
    from .versions import version_lt

    if after and before and after != before:
        return f"{before} -> {after}"
    if target and before and not after and version_lt(before, target):
        return f"{before} (цель {target})"
    return before or after or ""


def render_text(report, failed_only=False, show_skipped=False):
    lines = [f"Прогон {report['run_id']} [{report['mode']}] — {report.get('status')}",
             f"Итог: {summary_line(report)}", ""]
    by_bus = OrderedDict()
    for r in report["devices"]:
        if failed_only and r["ok"]:
            continue
        if r["status"] == "skipped" and not show_skipped and not failed_only \
                and r["reason"] in ("port_disabled", "device_disabled", "wbio_module"):
            continue
        by_bus.setdefault(r["bus"], []).append(r)
    for bus, rows in by_bus.items():
        cancelled = [r for r in rows if r["status"] == "cancelled"]
        rows = [r for r in rows if r["status"] != "cancelled"]
        bad = sum(1 for r in rows if not r["ok"])
        head = f"== {bus}  (устройств: {len(rows) + len(cancelled)}, проблем: {bad}"
        lines.append(head + (f", отменено: {len(cancelled)})" if cancelled else ")"))
        for r in sorted(rows, key=lambda x: (x["ok"], x["slave"] or 0)):
            fw = _version_change(r["fw_before"], r["fw_after"], r["fw_target"])
            bl = _version_change(r["bl_before"], r["bl_after"], r["bl_target"])
            mark = "  " if r["ok"] else "!!"
            lines.append(f" {mark} {r['raw_slave']:>6}  {r['label'][:34]:<34} {r['model'] or r['device_type']:<18}"
                         f" fw {fw:<18} bl {bl:<14} {r['code']}")
            if not r["ok"] or r.get("notes"):
                if r.get("message"):
                    lines.append(f"            {r['message']}")
                hint = REASON_HINTS.get(r.get("reason"))
                if hint and not r["ok"]:
                    lines.append(f"            -> {hint}")
                for note in r.get("notes", []):
                    lines.append(f"            * {note}")
        if cancelled:
            ids = ", ".join(r["raw_slave"] for r in sorted(cancelled, key=lambda x: x["slave"] or 0))
            lines.append(f"  -- отменено остановкой ({len(cancelled)}): {ids}")
            if report.get("mode") == "update":
                lines.append(f"     продолжить: uwb-fw-update update --resume {report.get('run_id')}")
            elif report.get("mode") == "recover":
                lines.append(f"     продолжить: uwb-fw-update recover --from-run {report.get('run_id')}")
        lines.append("")
    hidden = sum(1 for r in report["devices"] if r["status"] == "skipped"
                 and r["reason"] in ("port_disabled", "device_disabled", "wbio_module"))
    if hidden and not show_skipped and not failed_only:
        lines.append(f"(пропущено по конфигурации: {hidden}; показать — report --all)")
    return "\n".join(lines)
