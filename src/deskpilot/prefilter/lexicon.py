"""RU/EN dangerous-intent lexicon with fuzzy matching.

The lexicon only labels a request ("risky" or "malicious"). It never grants permission.

Matching rules:
- A two-part intent fires when an action word and a target word both match AND the target
  starts at most TARGET_AFTER tokens after the action or at most TARGET_BEFORE tokens before it.
  Phrase-only intents and action-only intents ignore position.
- Terms ending in "*" are prefix stems. Other single words use rapidfuzz ratio: words shorter
  than 5 chars must match exactly, short Latin words need 90, short Cyrillic words 80, longer
  words 85. Multi-word phrases need 85 over a window of the same token count.
- Opposite-verb guard: tokens in _NEVER_FUZZY (включи, enable, "turn on", readme, ...) never
  fuzzy-match another term, so "включи фаервол" is not "выключи фаервол".

Known limits: no negation handling ("do not delete"), no sentence boundaries beyond the token
window, split words ("dis able") are not joined, and obfuscation beyond look-alikes and
transliteration is not covered. The TF-IDF classifier is expected to cover such cases.
"""

from dataclasses import dataclass
from typing import Literal, NamedTuple

from rapidfuzz import fuzz, process

from deskpilot.prefilter.normalize import variants

Label = Literal["safe", "risky", "malicious"]

WORD_THRESHOLD = 80.0
PHRASE_THRESHOLD = 85.0
MIN_FUZZY_WORD_LEN = 5  # shorter single words must match exactly
LONG_WORD_LEN = 6
LONG_WORD_THRESHOLD = 85.0
LATIN_SHORT_THRESHOLD = 90.0
TARGET_AFTER = 6  # target may start this many tokens after the action word
TARGET_BEFORE = 3  # or this many tokens before it
_NEVER_FUZZY = frozenset(
    {
        "включи", "включить", "включай", "включите", "enable", "enabled", "activate",
        "turn on", "switch on", "readme", "render", "remain",
    }
)  # fmt: skip
_SEVERITY = {"malicious": 0, "risky": 1}


@dataclass(frozen=True)
class LexiconHit:
    intent: str
    label: Literal["risky", "malicious"]
    term: str
    matched: str
    score: float


@dataclass(frozen=True)
class _Intent:
    name: str
    label: Literal["risky", "malicious"]
    actions: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    phrases: tuple[str, ...] = ()


DELETE = (
    "удали", "удалить", "удалите", "удаляй", "удалим", "сотри", "сотрите", "стереть",
    "стирай", "снеси", "снести", "грохни", "грохнуть", "уничтожь", "уничтожить", "затри",
    "delete", "remove", "erase", "wipe", "nuke", "destroy", "purge", "shred",
)  # fmt: skip
DISABLE = (
    "отключи", "отключ*", "выключи", "выключ*", "останови", "остановить", "деактивируй",
    "вырубай", "вырубить", "disable", "turn off", "stop", "kill", "deactivate", "shut off", "off",
)  # fmt: skip
FORMAT = (
    "отформатир*", "форматир*", "форматн*", "форматни", "format", "reformat", "переформатир*",
)  # fmt: skip
MODIFY = (
    "измени", "изменить", "отредактируй", "правь", "поправь", "добавь", "создай", "запиши",
    "открой", "запусти", "edit", "change", "modify", "set", "add", "open", "run", "launch",
    "create", "import",
)  # fmt: skip
SEND = (
    "отправ*", "пришли", "перешли", "передай", "залей", "upload", "send", "email", "mail",
    "post", "exfiltrate", "leak", "forward",
)  # fmt: skip
SECRETS = (
    "пароли", "паролей", "пароль", "пароля", "passwords", "password", "credentials",
    "учетные данные", "учетных данных", "логины и пароли", "cookies", "cookie", "куки",
    "токены", "токен", "tokens", "token", "ssh ключи", "ssh key", "ssh keys", "api ключ",
    "api key", "api keys", "ключи api", "приватный ключ", "приватные ключи", "private key",
    "private keys", "профиль браузера", "профили браузера", "browser profile", "login data",
    "seed phrase", "env файл", "env file", "кошелек", "wallet",
)  # fmt: skip
MOVE = (
    "перемести", "переместить", "переименуй", "переименовать", "перезапиши", "перезаписать",
    "переложи", "move", "rename", "overwrite", "replace", "замени",
)  # fmt: skip
FILE_TARGETS = (
    "файл", "файла", "файлы", "файлов", "файле", "папку", "папка", "папки", "папке", "папок",
    "каталог", "директорию", "folder", "folders", "file", "files", "directory", "документ",
    "документы", "фото", "document", "documents", "photos",
)  # fmt: skip
CLEAN = ("очисти", "очист*", "почисти", "clean", "clear", "cleanup", "clean up")
COMPUTER = (
    "компьютер", "компьютера", "комп", "пк", "систему", "windows", "ноутбук", "computer", "pc",
    "laptop", "system", "machine", "машину",
)  # fmt: skip

INTENTS: tuple[_Intent, ...] = (
    # ---- malicious ----
    _Intent(
        "wipe_system_files",
        "malicious",
        DELETE,
        (
            "system32",
            "сыстем32",
            "систем32",
            "system 32",
            "windows",
            "c windows",
            "program files",
            "programdata",
            "bootmgr",
            "boot ini",
            "ntoskrnl",
            "hiberfil",
            "pagefile",
            "winsxs",
            "загрузчик",
            "системные файлы",
            "системную папку",
            "системные папки",
            "system files",
            "system folder",
            "windows folder",
            "папку windows",
            "boot files",
            "файлы загрузки",
        ),
    ),  # fmt: skip
    _Intent(
        "wipe_drive",
        "malicious",
        DELETE,
        (
            "диска c",
            "диске c",
            "диск c",
            "диска с",
            "диске с",
            "все на c",
            "все с c",
            "everything on c",
            "all files on c",
            "c drive",
            "drive c",
            "весь диск",
            "целый диск",
            "все диски",
            "все данные с диска",
            "whole disk",
            "entire disk",
            "entire drive",
            "hard drive",
            "hard disk",
            "жесткий диск",
            "жесткого диска",
            "системный диск",
            "системного диска",
            "system drive",
            "all drives",
        ),
    ),  # fmt: skip
    _Intent(
        "format_drive",
        "malicious",
        FORMAT,
        (
            "диск",
            "диска",
            "диске",
            "диски",
            "флешку",
            "флешка",
            "флешки",
            "флешке",
            "drive",
            "disk",
            "usb",
            "ssd",
            "hdd",
            "partition",
            "раздел",
            "раздела",
            "volume",
            "носитель",
            "накопитель",
            "жесткий",
            "жесткого",
            "c",
            "d",
            "e",
            "f",
        ),
    ),  # fmt: skip
    _Intent(
        "destructive_command",
        "malicious",
        (),
        (),
        (
            "rm rf",
            "del s q",
            "del f s q",
            "rd s q",
            "rmdir s q",
            "diskpart clean",
            "cipher w",
            "remove item recurse force",
            "dd if dev zero",
            "clean all",
            "dd if dev sda",
        ),
    ),  # fmt: skip
    _Intent(
        "disable_defender",
        "malicious",
        DISABLE,
        (
            "защитник",
            "защитника",
            "защитнику",
            "defender",
            "антивирус",
            "антивируса",
            "антивирусник",
            "antivirus",
            "брандмауэр",
            "брандмауэра",
            "файервол",
            "firewall",
            "uac",
            "smartscreen",
            "контроль учетных записей",
            "windows security",
            "безопасность windows",
            "защиту windows",
            "real time protection",
            "защиту в реальном времени",
            "антивирусную защиту",
        ),
    ),  # fmt: skip
    _Intent(
        "disable_updates",
        "malicious",
        DISABLE,
        (
            "обновления",
            "обновлений",
            "updates",
            "windows update",
            "автообновление",
            "обновления windows",
            "wuauserv",
        ),
    ),  # fmt: skip
    _Intent(
        "edit_registry",
        "malicious",
        MODIFY + DELETE,
        (
            "реестр",
            "реестра",
            "реестре",
            "registry",
            "regedit",
            "hklm",
            "hkcu",
            "hkey local machine",
            "hkey current user",
        ),
        ("reg add", "reg delete"),
    ),  # fmt: skip
    _Intent(
        "edit_boot_config",
        "malicious",
        MODIFY + DELETE,
        (
            "bcdedit",
            "bootloader",
            "boot loader",
            "загрузчик",
            "загрузчика",
            "uefi",
            "mbr",
            "secure boot",
            "boot sector",
            "загрузочный сектор",
            "boot configuration",
        ),
    ),  # fmt: skip
    _Intent(
        "persistence_autorun",
        "malicious",
        (
            "добавь",
            "создай",
            "настрой",
            "поставь",
            "пропиши",
            "add",
            "create",
            "set up",
            "put",
            "register",
            "make",
            "помести",
            "положи",
            "set",
        ),
        (
            "автозагрузку",
            "автозагрузка",
            "автозагрузки",
            "автозапуск",
            "автозапуске",
            "startup",
            "autorun",
            "run key",
            "при загрузке системы",
            "при входе в систему",
            "on boot",
            "at logon",
            "at startup",
            "on startup",
            "на старте системы",
        ),
    ),  # fmt: skip
    _Intent(
        "hidden_task",
        "malicious",
        ("создай", "добавь", "create", "add", "make", "set up"),
        (
            "скрытую задачу",
            "скрытое задание",
            "hidden task",
            "hidden scheduled task",
            "невидимую задачу",
            "задачу скрытно",
            "scheduled task hidden",
        ),
    ),  # fmt: skip
    _Intent(
        "download_execute",
        "malicious",
        ("скачай", "скач*", "загрузи", "download", "fetch", "curl", "wget"),
        (
            "и запусти",
            "и выполни",
            "запусти его",
            "and run",
            "and execute",
            "run it",
            "execute it",
            "invoke expression",
            "iex",
            "and install silently",
            "и установи тихо",
        ),
        (
            "curl bash",
            "wget sh",
            "curl sh",
            "irm iex",
            "iwr iex",
            "curl iex",
            "downloadstring",
            "iex",
            "invoke expression",
        ),
    ),  # fmt: skip
    _Intent(
        "steal_secrets",
        "malicious",
        (
            "укради",
            "украсть",
            "украд*",
            "украл*",
            "своруй",
            "выкради",
            "дампни",
            "вытащи",
            "достань",
            "извлеки",
            "экспортируй",
            "скопируй",
            "steal",
            "dump",
            "extract",
            "grab",
            "harvest",
            "export",
            "copy",
            "hijack",
            "покажи",
            "показать",
            "show",
            "reveal",
            "display",
            "выведи",
            "list",
            "копию",
            "копия",
            "бэкап",
            "backup",
            "дубликат",
        ),
        SECRETS,
        ("dump lsass", "mimikatz", "lsass", "sam hive", "login data"),
    ),  # fmt: skip
    _Intent("exfiltrate_secrets", "malicious", SEND, SECRETS),
    _Intent(
        "keylogger",
        "malicious",
        (
            "создай",
            "установи",
            "запусти",
            "напиши",
            "включи",
            "install",
            "run",
            "create",
            "write",
            "start",
            "log",
            "record",
            "запиши",
            "записывай",
            "перехватывай",
            "capture",
        ),
        (
            "кейлоггер",
            "keylogger",
            "key logger",
            "клавиатурный шпион",
            "перехватчик клавиш",
            "нажатия клавиш",
            "нажатий клавиш",
            "keystrokes",
            "keystroke logger",
        ),
    ),  # fmt: skip
    _Intent(
        "ransomware",
        "malicious",
        ("зашифр*", "шифруй", "encrypt", "lock", "заблокируй"),
        (
            "все файлы",
            "мои файлы",
            "файлы пользователя",
            "все документы",
            "мои документы",
            "all files",
            "user files",
            "my files",
            "my documents",
            "все фото",
            "диск",
            "drive",
        ),
        (
            "ransomware",
            "шифровальщик",
            "вымогатель",
            "ransom note",
            "записка о выкупе",
            "требование выкупа",
            "требуй выкуп",
            "demand ransom",
        ),
    ),  # fmt: skip
    _Intent(
        "clear_logs",
        "malicious",
        CLEAN + DELETE,
        (
            "журнал событий",
            "журналы событий",
            "журналы windows",
            "журнал безопасности",
            "event log",
            "event logs",
            "логи событий",
            "системные логи",
            "security log",
            "журнал аудита",
            "audit log",
            "историю команд",
            "command history",
            "powershell history",
            "bash history",
        ),
        ("wevtutil cl",),
    ),  # fmt: skip
    _Intent(
        "hide_tracks",
        "malicious",
        (),
        (),
        (
            "замести следы",
            "заметай следы",
            "скрой следы",
            "спрячь следы",
            "затри следы",
            "hide tracks",
            "hide my tracks",
            "cover tracks",
            "cover my tracks",
            "erase traces",
            "delete traces",
            "remove traces",
            "clear traces",
            "стереть следы",
            "удали следы",
            "уничтожь следы",
            "hide evidence",
            "anti forensics",
        ),
    ),  # fmt: skip
    _Intent(
        "bypass_security",
        "malicious",
        (
            "обойди",
            "обойти",
            "взломай",
            "взломать",
            "bypass",
            "crack",
            "hack",
            "circumvent",
            "evade",
        ),
        (
            "защиту",
            "защита",
            "защиты",
            "антивирус",
            "авторизацию",
            "авторизации",
            "пароль",
            "пароля",
            "uac",
            "аутентификацию",
            "security",
            "authentication",
            "password",
            "login",
            "firewall",
            "wifi",
            "wi fi",
            "вайфай",
            "учетку",
            "аккаунт",
            "account",
            "antivirus",
            "защитника",
            "defender",
            "лицензию",
        ),
        ("privilege escalation", "повысь привилегии"),
    ),  # fmt: skip
    _Intent(
        "malware_creation",
        "malicious",
        (
            "напиши",
            "создай",
            "сделай",
            "разработай",
            "сгенерируй",
            "write",
            "create",
            "make",
            "build",
            "develop",
            "generate",
            "code",
            "compile",
        ),
        (
            "вирус",
            "virus",
            "malware",
            "троян",
            "trojan",
            "червь",
            "worm",
            "backdoor",
            "бэкдор",
            "rootkit",
            "руткит",
            "spyware",
            "шпионскую программу",
            "stealer",
            "стилер",
            "ботнет",
            "botnet",
            "эксплойт",
            "payload",
        ),
    ),  # fmt: skip
    # ---- risky ----
    _Intent("delete_files", "risky", DELETE),
    _Intent("move_rename_overwrite", "risky", MOVE, FILE_TARGETS),
    _Intent(
        "bulk_operation",
        "risky",
        MOVE + DELETE + ("заархивируй", "archive", "сожми"),
        (
            "все файлы",
            "всех файлов",
            "все в загрузках",
            "все из загрузок",
            "всю папку",
            "all files",
            "every file",
            "все документы",
            "все фото",
            "все содержимое",
            "содержимое папки",
            "everything in",
            "all documents",
            "all photos",
        ),
    ),  # fmt: skip
    _Intent(
        "cleanup_folder",
        "risky",
        CLEAN,
        (
            "загрузки",
            "загрузок",
            "загрузках",
            "рабочий стол",
            "рабочего стола",
            "downloads",
            "desktop",
            "папку",
            "папка",
            "folder",
            "документы",
        ),
    ),  # fmt: skip
    _Intent(
        "empty_recycle_bin",
        "risky",
        ("очисти", "очист*", "опустоши", "освободи", "empty", "clear", "clean", "purge"),
        ("корзину", "корзина", "корзины", "корзине", "recycle bin", "trash", "bin"),
    ),  # fmt: skip
    _Intent(
        "install_uninstall",
        "risky",
        (
            "установи",
            "установить",
            "поставь",
            "инсталл*",
            "деинсталл*",
            "install",
            "uninstall",
            "удали",
        ),
        (
            "программу",
            "приложение",
            "программа",
            "драйвер",
            "пакет",
            "расширение",
            "софт",
            "app",
            "program",
            "software",
            "driver",
            "package",
            "extension",
            "plugin",
            "дополнение",
            "игру",
            "game",
            "браузер",
            "browser",
        ),
    ),  # fmt: skip
    _Intent(
        "run_script_command",
        "risky",
        ("запусти", "выполни", "run", "execute"),
        (
            "скрипт",
            "script",
            "команду",
            "command",
            "powershell",
            "cmd",
            "bat",
            "батник",
            "ps1",
            "exe",
            "командную строку",
            "terminal",
            "терминал",
            "bash",
            "msi",
            "установщик",
        ),
    ),  # fmt: skip
    _Intent(
        "kill_process", "risky", ("убей", "убить", "kill", "terminate", "taskkill", "прикончи")
    ),
    _Intent(
        "stop_process",
        "risky",
        ("заверш*", "останови", "stop", "end"),
        ("процесс", "процессы", "process", "службу", "service", "pid", "процесса"),
    ),  # fmt: skip
    _Intent(
        "change_settings",
        "risky",
        (
            "измени",
            "изменить",
            "поменяй",
            "смени",
            "включи",
            "выключи",
            "отключи",
            "переключи",
            "настрой",
            "change",
            "set",
            "toggle",
            "enable",
            "disable",
            "turn on",
            "turn off",
            "switch",
        ),
        (
            "настройки",
            "настройку",
            "параметры",
            "настроек",
            "settings",
            "setting",
            "яркость",
            "громкость",
            "обои",
            "тему",
            "dns",
            "proxy",
            "прокси",
            "wifi",
            "wi fi",
            "bluetooth",
            "режим",
            "mode",
            "разрешение",
            "brightness",
            "volume",
            "wallpaper",
            "theme",
            "язык",
            "language",
            "power plan",
        ),
    ),  # fmt: skip
    _Intent(
        "send_message",
        "risky",
        (
            "отправ*",
            "пришли",
            "напиши",
            "ответь",
            "send",
            "post",
            "email",
            "tweet",
            "опубликуй",
            "reply",
            "forward",
            "перешли",
        ),
        (
            "сообщение",
            "сообщения",
            "письмо",
            "письма",
            "почту",
            "email",
            "mail",
            "message",
            "tweet",
            "пост",
            "комментарий",
            "telegram",
            "телеграм",
            "whatsapp",
            "slack",
            "discord",
            "sms",
            "смс",
            "comment",
        ),
    ),  # fmt: skip
    _Intent(
        "shutdown_restart",
        "risky",
        (
            "выключи",
            "выключить",
            "перезагрузи",
            "перезагруз*",
            "перезапусти",
            "restart",
            "reboot",
            "hibernate",
            "shut down",
        ),
        COMPUTER,
        ("shutdown", "shut down", "reboot", "hibernate", "заверши работу"),
    ),  # fmt: skip
)


def _word_cutoff(word: str) -> float:
    if len(word) < MIN_FUZZY_WORD_LEN:
        return 100.0
    if len(word) <= LONG_WORD_LEN:
        return LATIN_SHORT_THRESHOLD if word.isascii() else WORD_THRESHOLD
    return LONG_WORD_THRESHOLD


class _Match(NamedTuple):
    score: float
    text: str
    start: int
    size: int
    term: str


class _Matcher:
    """Term lookup over the tokens of one text variant, memoized per term."""

    def __init__(self, text: str) -> None:
        self._tokens = text.split()
        self._windows: dict[int, list[str]] = {}
        self._memo: dict[str, tuple[_Match, ...]] = {}

    def _windows_of(self, size: int) -> list[str]:
        if size not in self._windows:
            n = len(self._tokens)
            self._windows[size] = [
                " ".join(self._tokens[i : i + size]) for i in range(n - size + 1)
            ]
        return self._windows[size]

    def find(self, term: str) -> tuple[_Match, ...]:
        if term not in self._memo:
            self._memo[term] = self._lookup(term)
        return self._memo[term]

    def _lookup(self, term: str) -> tuple[_Match, ...]:
        if term.endswith("*"):
            stem = term[:-1]
            return tuple(
                _Match(100.0, t, i, 1, term)
                for i, t in enumerate(self._tokens)
                if t.startswith(stem)
            )
        size = len(term.split())
        cutoff = _word_cutoff(term) if size == 1 else PHRASE_THRESHOLD
        found = process.extract(
            term, self._windows_of(size), scorer=fuzz.ratio, score_cutoff=cutoff, limit=None
        )
        return tuple(
            _Match(float(score), window, i, size, term)
            for window, score, i in found
            if window == term or window not in _NEVER_FUZZY
        )

    def all(self, terms: tuple[str, ...]) -> list[_Match]:
        return [m for t in terms for m in self.find(t)]


def _near(act: _Match, tgt: _Match) -> bool:
    after = tgt.start - (act.start + act.size)
    before = act.start - (tgt.start + tgt.size)
    return 0 <= after <= TARGET_AFTER or 0 <= before <= TARGET_BEFORE


def _check(intent: _Intent, matcher: _Matcher) -> LexiconHit | None:
    candidates: list[tuple[float, str, str]] = []
    acts = matcher.all(intent.actions)
    if acts and not intent.targets:
        a = max(acts, key=lambda m: m.score)
        candidates.append((a.score, a.text, a.term))
    for a in acts if intent.targets else ():
        for t in matcher.all(intent.targets):
            if _near(a, t):
                score = min(a.score, t.score)
                candidates.append((score, f"{a.text} {t.text}", f"{a.term} + {t.term}"))
    candidates += [(m.score, m.text, m.term) for m in matcher.all(intent.phrases)]
    if not candidates:
        return None
    score, matched, term = max(candidates, key=lambda c: c[0])
    return LexiconHit(intent.name, intent.label, term, matched, round(score, 1))


def scan(text: str) -> tuple[LexiconHit, ...]:
    """Return one hit per matched intent, most severe and highest-scoring first."""
    best: dict[str, LexiconHit] = {}
    for variant in variants(text):
        matcher = _Matcher(variant)
        for intent in INTENTS:
            hit = _check(intent, matcher)
            if hit and (hit.intent not in best or hit.score > best[hit.intent].score):
                best[hit.intent] = hit
    return tuple(sorted(best.values(), key=lambda h: (_SEVERITY[h.label], -h.score, h.intent)))


def strongest(hits: tuple[LexiconHit, ...]) -> Label:
    """Most severe label among the hits ("safe" when there are none)."""
    if any(h.label == "malicious" for h in hits):
        return "malicious"
    return "risky" if hits else "safe"
