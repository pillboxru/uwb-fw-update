# uwb-fw-update — разработка

Утилита массового обновления прошивок Modbus-устройств Wiren Board: пакет `uwbfwup/` (Python 3.9+, без зависимостей, кроме `python3-serial`). Работает на контроллере. Приложение неофициальное (не от Wiren Board) — это должно быть видно в документации. Пользовательская документация — [README.md](README.md). Скилл для агента, который запускает утилиту на контроллере, — [.claude/skills/uwb-fw-update/](.claude/skills/uwb-fw-update/SKILL.md).

## После каждого изменения кода

```sh
python -m unittest discover -s tests -t .     # железо не нужно: tests/fake_device.py, tests/fake_broker.py
python tools/build.py                         # -> ./uwb-fw-update (zipapp, единый файл; лежит в git)
```

Пересобранный `uwb-fw-update` коммитьте вместе с изменением кода. Сборка воспроизводима, так что без изменений в `uwbfwup/` файл не меняется.

Релиз на GitHub (отсюда утилита узнаёт о новой версии и обновляет себя: `version --check`, `self-update`):

```sh
# поднять __version__ в uwbfwup/__init__.py -> тесты -> build -> commit -> git push
python tools/release.py        # gh release create v<версия>: uwb-fw-update + uwb-fw-update.sha256
```

Деплой на контроллер — только в `/mnt/data/uwb-fw-update/`, не в `/usr/local/bin` и не в `/root`:

```sh
scp uwb-fw-update root@<HOST>:/mnt/data/uwb-fw-update/uwb-fw-update
ssh root@<HOST> chmod 755 /mnt/data/uwb-fw-update/uwb-fw-update
```

## Работа с живым контроллером

Контроллеры — это живые объекты автоматизации. Свободно, без вопросов, выполняйте только чтение: `list`, `status`, `report`, `logs`, `runs`, `cache list/verify`, `check --access rpc`. **Сначала спросите** перед всем, что останавливает `wb-mqtt-serial` (`check`/`update` в режиме `direct`), прошивает устройства, прерывает запись или перезагружает контроллер. В вопросе назовите последствия.

## Где хранить знания о проекте

Всё, что стоит помнить между сессиями, записывайте в файлы проекта, а не в автопамять агента (у Claude это `~/.claude/projects/.../memory/`): правила, соглашения, процесс сборки и тестов — в этот `AGENTS.md`. Пароли и ключи не записывайте никуда.

## Соглашения

- Сообщения, логи, комментарии и документация — на русском.
- Коды выхода: 0 — ок, 1 — есть проблемные устройства, 2 — параметры, 3 — окружение (`runner.EXIT_*`).
- Изменилось поведение, флаги или статусы — обновите README и, если это касается агента, `.claude/skills/uwb-fw-update/` (SKILL.md и `references/troubleshooting.md`).
- Причины ошибок и подсказки к ним собраны в `uwbfwup/report.py` (`REASON_HINTS`).
