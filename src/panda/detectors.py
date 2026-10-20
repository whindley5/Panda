"""Deterministic and local statistical PII recognisers."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Protocol

from panda.limits import check_text
from panda.model import Confidence, Detection, PIIKind
from panda.normalise import to_search_text

_CN_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_CN_ID_CHECK = "10X98765432"


def _cn_id_is_valid(value: str) -> bool:
    """Verify a mainland Chinese ID against its ISO 7064 MOD 11-2 check digit.

    Worth doing rather than trusting the shape: any 18 digit run would otherwise
    match, and long account numbers are common in these documents.
    """
    if len(value) != 18 or not value[:17].isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(value[:17], _CN_ID_WEIGHTS, strict=True))
    return _CN_ID_CHECK[total % 11] == value[17].upper()


def _date_is_real(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
    except ValueError:
        return False
    # A date far outside a plausible birth range is more likely a reference number.
    return 1900 <= year <= date.today().year


# Patterns run against search text, where full-width characters have already been
# folded to ASCII at identical offsets. See normalise.to_search_text.
# ASCII only: Python's \w matches Han, which let "邮箱是john@x.com" swallow the prose.
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")

# Digit groups may be split by single spaces or hyphens anywhere ("0392 555 017",
# "+86 137 1234 0987"), so each shape is a fixed digit count with optional separators.
# A trailing (?!\d) rather than (?![ -]?\d) lets a phone sit directly before another
# number in the same message.
_PHONE_AU = re.compile(
    r"(?<![\d+-])(?:"
    r"(?:\+|00)?61[ -]?(?:\(0\)[ -]?)?[2-478](?:[ -]?\d){8}"
    r"|\(0[2-478]\)[ -]?\d(?:[ -]?\d){7}"
    r"|0[ -]?[2-478](?:[ -]?\d){8}"
    r")(?!\d)"
)
# Mainland Chinese mobile (1, then 3-9, then nine more digits) and hyphenated landline.
_PHONE_CN = re.compile(
    r"(?<![\d+-])(?:(?:(?:\+|00)?86[ -]?)?1[3-9]\d(?:[ -]?\d){8}"
    r"|0(?:10|2\d|[3-9]\d{2})-\d{7,8})(?!\d)"
)

# A sentence-ending full stop may follow; another date component may not.
_DOB_SLASH = re.compile(
    r"(?<![\d/.-])(\d{1,2})[ ]?[/.-][ ]?(\d{1,2})[ ]?[/.-][ ]?(\d{4})(?![\d/-]|\.\d)"
)
_DOB_ISO = re.compile(r"(?<![\d/.-])(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})(?![\d/-]|\.\d)")
_DOB_HAN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|"
    "december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_MONTH_NUMBER = {name: index for index, name in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_DOB_WRITTEN_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:[ \t]+of)?[ \t-]+({_MONTHS})\.?,?[ \t-]+(\d{{4}})\b",
    re.IGNORECASE,
)
_DOB_WRITTEN_MDY = re.compile(
    rf"\b({_MONTHS})\.?[ \t]+(\d{{1,2}})(?:st|nd|rd|th)?,?[ \t]+(\d{{4}})\b", re.IGNORECASE
)
_DOB_SHORT_YEAR = re.compile(
    rf"(?<![\d/.-])(\d{{1,2}})[/. -]((?:\d{{1,2}})|(?:{_MONTHS}))[/. -](\d{{2}})(?![\d/-]|\.\d)",
    re.IGNORECASE,
)
_ORDINAL_WORDS = {
    word: index for index, word in enumerate((
        "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
        "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth"
    ).split(), start=1)
}
_DOB_ORDINAL = re.compile(
    rf"\b((?:twenty|thirty)[- ])?({'|'.join(_ORDINAL_WORDS)})[ \t]+of[ \t]+({_MONTHS})"
    rf",?[ \t]+(\d{{4}})\b",
    re.IGNORECASE,
)
_DOB_CONTEXT = re.compile(
    r"(?:\bdob\b|d\.o\.b|date\s+of\s+birth|\bborn\b|\bbirth(?:day|date)?\b|"
    r"出生(?:日期|年月)?|生日|生于)",
    re.IGNORECASE,
)
# Words that make an old date something other than a birth date.
_NON_BIRTH_CONTEXT = re.compile(
    r"(?:appointment|meeting|trade|traded|transaction|transfer|deposit|withdraw|order|"
    r"invoice|statement|dated|opened|closed|since|expir|issued|valid|交易|转账|入金|出金|开户)",
    re.IGNORECASE,
)
# A customer's birth date is at least this many years back; anything more recent is a
# transaction, appointment or export timestamp and needs an explicit birth cue.
_MIN_BIRTH_AGE = 10
_UNCUED_BIRTH_AGE = 16

_CN_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")

# Government and document identifiers. Most have no reliable intrinsic shape, so a
# label must appear shortly before the value, on the same line or in the detection
# context (a form's label cell). The window tolerates "Nationality / passport:
# Australian / PA7654321" and "Driver licence number (SA): S1234567".
_ID_LABEL = re.compile(
    r"(?:passport|licen[cs]e|\bDL\b|medicare|\bTFN\b|tax\s+file|\bABN\b|\bACN\b|"
    r"\bID\b|identity|identification|national\s+id|centrelink|\bCRN\b|\bIRD\b|\bCSN\b|"
    r"\bHIN\b|\bSRN\b|\bJP\b|adviser[ \t]+(?:no|number)|employee[ \t]+(?:no|number)|"
    r"证件|护照|驾照|驾驶证|身份证|港澳通行证|居民身份|社保|税号|纳税)",
    re.IGNORECASE,
)
# A licence may lead with its state ("VIC 0387 5521"), a CRN ends in a letter.
_ID_VALUE = re.compile(
    r"(?<![A-Za-z0-9-])(?:[A-Z]{1,2}\d{6,9}|(?:(?:NSW|VIC|QLD|SA|WA|TAS|ACT|NT)[ \t]+)?"
    r"\d(?:[ -]?\d){5,11}[A-Z]?)(?![A-Za-z0-9:])"
)
# Hong Kong identity card: one or two letters, six digits, check digit in brackets.
_HKID = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\d{6}\([0-9A]\)")
_NOT_ID_LABEL = re.compile(r"(?:AFSL|ACL|holder[ \t]+no\.?)[ \t]*[:：#]?[ \t]*$", re.IGNORECASE)
_ID_WINDOW = 48


def _tfn_is_valid(digits: str) -> bool:
    weights = (1, 4, 3, 7, 5, 8, 6, 9, 10)
    if len(digits) != 9:
        return False
    return sum(int(d) * w for d, w in zip(digits, weights, strict=True)) % 11 == 0


def _medicare_is_valid(digits: str) -> bool:
    weights = (1, 3, 7, 9, 1, 3, 7, 9)
    return (
        len(digits) in (10, 11) and digits[0] in "23456"
        and sum(int(d) * w for d, w in zip(digits[:8], weights, strict=True)) % 10 == int(digits[8])
    )


_SURNAME_ACTION = re.compile(
    r"\b([A-Z][a-z]?[’'][A-Z][a-z]+)(?=\s+(?:requested|called|contacted|replied)\b)"
)


# Words that follow a greeting or title but are not names ("Hi there", "Dear Sir").
# Stored lower-case; spaCy's stop words are added at match time.
_NOT_NAME_WORDS = frozenset({
    "team", "there", "all", "everyone", "everybody", "sir", "madam", "maam", "again",
    "guys", "mate", "mates", "folks", "friend", "friends", "customer", "client",
    "support", "service", "agent", "staff", "sorry", "thanks",
    "thank", "hello", "hi", "hey", "dear", "regards", "cheers", "morning", "afternoon",
    "evening", "day", "night", "sure", "yes", "no", "ok", "okay", "great", "please",
    "welcome", "bye", "goodbye", "mr", "mrs", "ms", "miss", "mx", "dr", "prof",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december", "australia", "china", "sydney", "melbourne",
    # Departments and roles addressed like people ("Hi Compliance,", "Dear Onboarding").
    "compliance", "operations", "onboarding", "accounts", "finance", "legal", "admin",
    "desk", "trader", "trading", "account", "sales", "marketing", "helpdesk", "services",
    "management", "manager", "director", "officer", "board", "audit", "risk", "payments",
    "settlements", "partnerships", "adviser", "advisor", "reception", "hr", "it",
    # Document and form titles ("Beneficiary Nomination Form", "Trustee Application").
    "application", "form", "nomination", "notification", "estate", "summary", "request",
    "declaration", "agreement", "statement", "report", "notice", "letter", "details",
    "information", "record", "confirmation", "authority", "review", "update", "change",
    "closure", "transfer", "complaint", "feedback", "certificate", "profile",
    "individual", "joint", "company", "supplementary", "section", "part", "schedule",
    # Role and relationship words used as cues must never become names themselves.
    "beneficiary", "nominee", "executor", "trustee", "referrer", "spouse", "husband",
    "wife", "partner", "son", "daughter", "mother", "father", "mum", "dad", "brother",
    "sister", "neighbour", "accountant", "lawyer", "solicitor", "guardian", "carer",
    "attorney", "deceased", "holder", "contact", "visitor", "guest", "member", "user",
    "session", "queue", "chat", "log", "export", "certified", "smsf",
    # Form field labels, which sit under a "Name" column header in label/value tables.
    "id", "passport", "address", "phone", "mobile", "email", "number", "dob", "date",
    "birth", "tfn", "abn", "occupation", "nationality", "gender", "signature", "signed",
    "residential", "postal", "home", "work", "fax", "licence", "license", "medicare",
    "bsb", "bank", "country", "citizenship", "employer", "status", "type", "item",
    "detail", "value", "field", "reference", "relationship", "percentage", "share",
    # Institutions and places that the English model tags as people.
    "trust", "superannuation", "super", "fund", "family", "pty", "ltd", "limited",
    "centre", "center", "tower", "building", "house", "plaza", "court", "chambers",
    "centrelink", "ato", "asic", "austrac", "ird", "hsbc", "anz", "nab", "westpac",
    "commbank", "cba", "dbs", "citibank", "macquarie", "sharesies", "commsec",
    "brokers", "broker", "securities", "markets", "group", "international", "holdings",
})


def _is_name_word(word: str) -> bool:
    from spacy.lang.en.stop_words import STOP_WORDS

    lowered = word.casefold()
    return (
        lowered not in _NOT_NAME_WORDS and lowered not in STOP_WORDS
        and lowered not in _ORGANISATION.get().brands
    )


# A capitalised given name, optionally followed by one more capitalised word. The
# name group is validated word by word afterwards, so "Thanks Sheldon Have a nice day"
# keeps "Sheldon" and drops the stop word "Have".
# Romanised Korean and Chinese given names are often hyphenated in lower case
# ("Hyun-woo", "Min-jun"), so the part after a hyphen may start either way.
_CAP_NAME = r"[A-Z][a-z]+(?:['’][A-Z][a-z]+)?(?:-[A-Za-z][a-z]+)?"
_CAP_NAMES = rf"{_CAP_NAME}(?:[ \t]+{_CAP_NAME})?"
_TITLES = r"(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof)"
_SALUTATION = re.compile(
    r"\b(?:[Hh]i|[Hh]ello|[Hh]ey|[Dd]ear|[Tt]hanks|[Tt]hank[ \t]?you|[Cc]heers|[Bb]ye|"
    r"[Gg]ood(?:bye|[ \t]+(?:morning|afternoon|evening|day|night))|[Mm]orning|[Ss]orry|"
    r"[Rr]egards|[Ss]incerely|[Ww]elcome)"
    # A sign-off name often sits on the next line ("Thanks\nDan").
    rf"[ \t]*,?(?:[ \t]+|[ \t]*\r?\n[ \t]*)(?!{_TITLES}\b)(?P<name>{_CAP_NAMES})\b"
)
_TITLED = re.compile(rf"\b{_TITLES}\.?[ \t]+(?P<name>{_CAP_NAMES})\b")
_SELF_INTRO = re.compile(
    r"(?:\b[Mm]y[ \t]+(?:full[ \t]+)?name[ \t]+is|\b[Nn]ame[ \t]+is|\b[Cc]all[ \t]+me|"
    r"\b[Tt]his[ \t]+is|\bI[ \t]+am|\bI['’]m|\b[Kk]nown[ \t]+as|\b[Gg]oes[ \t]+by|"
    r"\b[Kk]nows?[ \t]+(?:you|him|her|me|them)[ \t]+as)"
    rf"[ \t]+(?P<name>{_CAP_NAMES})\b"
)
# People introduced by their role or relationship ("Customer Olumide Balogun",
# "my wife Kate O'Brien", "beneficiary Tuyet Nguyen").
_ROLE_CUE = re.compile(
    r"\b(?:[Cc]ustomer|[Cc]lient|[Bb]eneficiary|[Nn]ominee|[Ee]xecutor|[Tt]rustee|[Rr]eferrer|"
    r"[Ss]pouse|[Hh]usband|[Ww]ife|[Pp]artner|[Ss]on|[Dd]aughter|[Mm]other|[Ff]ather|[Mm]um|"
    r"[Dd]ad|[Bb]rother|[Ss]ister|[Ff]riend|[Nn]eighbour|[Aa]ccountant|[Ll]awyer|[Ss]olicitor|"
    r"[Gg]uardian|[Cc]arer|[Aa]ttorney|[Dd]eceased|[Hh]older|[Cc]ontact)"
    rf"(?:['’]s)?[ \t]*[:,]?[ \t]+(?!{_TITLES}\b)(?P<name>{_CAP_NAMES})\b"
)
# An alias in brackets straight after a full name: "Vanessa Lo-Brennan (VLB)",
# "Olumide Balogun (Ola)".
_BRACKET_ALIAS = re.compile(
    rf"{_CAP_NAME}(?:[ \t]+{_CAP_NAME})+[ \t]*[(（](?P<name>[A-Z]{{2,4}}|[A-Z][a-z]{{1,11}})[)）]"
)
_INITIALS_SALUTATION = re.compile(
    r"\b(?:Dear|Hi|Hello)[ \t]+(?P<name>[A-Z]{2,3})\b(?=[ \t]*[,!\n])"
)
# Aliases, which are often initials ("known as JP") or an initial and a surname
# ("he sometimes signs J. Park", "my fund shows me as R. Venkataraman").
_ALIAS = re.compile(
    r"\b(?:[Kk]nown[ \t]+as|[Gg]oes[ \t]+by|[Rr]eferred[ \t]+to[ \t]+as|[Nn]ickname[ \t]*[:：]?|"
    r"[Ss]igns?(?:[ \t]+as)?|[Ss]igned(?:[ \t]+as)?|[Aa]\.?[Kk]\.?[Aa]\.?|"
    r"(?:[Ss]hows?|[Ll]ists?|[Hh]as)[ \t]+(?:me|him|her|them)[ \t]+as|[Ll]isted[ \t]+as)"
    rf"[ \t]+(?P<name>(?:[A-Z]\.[ \t]?){{1,2}}{_CAP_NAME}|[A-Z]{{2,4}})(?![A-Za-z])"
)
# Form-style labels, where upper-case names are common ("Name: SHELDON COOPER").
_NAME_LABEL = re.compile(
    r"\b(?:(?:[Ff]ull|[Ff]irst|[Ll]ast|[Gg]iven|[Mm]iddle|[Pp]referred|[Ff]amily|[Cc]ustomer|"
    r"[Cc]lient|[Aa]ccount(?:[ \t]+[Hh]older)?|[Hh]older|[Bb]eneficiary|[Rr]eferrer)[ \t]+)?"
    r"(?:[Nn]ame|NAME|[Ss]urname|SURNAME|[Nn]ickname)(?:\(s\))?[ \t]*[:：]?[ \t]*\r?\n?[ \t]*"
    rf"(?P<name>{_CAP_NAMES}|[A-Z]{{2,}}(?:[- \t]+[A-Z]{{2,}}){{0,2}})\b"
)
# Names spelled out letter by letter: "K A R I M I", "A-M-I-R", "L-I-U Y-A-N-G".
_SPELLED_NAME = re.compile(
    r"(?<![A-Za-z])[A-Z](?:[ .-][A-Z]){2,}(?:[ ]{1,2}[A-Z](?:[ .-][A-Z])+)*\.?(?![A-Za-z])"
)

# Chat exports label every message with ``[Role]handle [<date>] <time>``. The handle is
# the participant's chosen display name or platform identifier, and it repeats on
# every line, so missing it leaks the customer's name dozens of times per transcript.
# Extraction can glue a header onto the end of the previous line, so no line anchor.
_SPEAKER_HEADER = re.compile(
    r"\[(?P<role>[^\[\]\n]{1,24})\][ \t]*<?(?P<handle>[^\s\[\]<>][^\n\[\]<>]{0,60}?)>?[ \t]*"
    r"(?=(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]+)?\d{1,2}:\d{2})"
)
_AUTOMATED_ROLES = re.compile(
    r"^(?:system|ai|bot|robot|auto\w*|guest|visitor|anonymous|unknown|系统|机器人|智能\w*|访客|游客)$",
    re.I,
)
_CUSTOMER_ROLES = re.compile(
    r"^(?:customer|client|user|visitor|guest|member|me|客户|用户|访客|会员|顾客)$", re.I
)


def _is_name_like_handle(handle: str) -> bool:
    """Letters dominate a chosen display name ("NadiaK", "jenny_wu88", "娜娜"), while
    platform IDs and staff codes are mostly digits ("8f3a91c2", "AU-0031")."""
    letters = sum(character.isalpha() for character in handle)
    return letters >= 2 and letters * 2 > len(handle.replace(" ", ""))


def _speaker_handles(text: str) -> Iterator[Detection]:
    for match in _SPEAKER_HEADER.finditer(text):
        role = match.group("role").strip()
        handle = match.group("handle")
        if _AUTOMATED_ROLES.match(role) or _AUTOMATED_ROLES.match(handle) or _EMAIL.fullmatch(
            to_search_text(handle)
        ):
            continue
        # Phone numbers used as handles belong to the phone recogniser, whose
        # replacement keeps the field looking like a number.
        compact = handle.replace(" ", "")
        if _PHONE_AU.fullmatch(compact) or _PHONE_CN.fullmatch(compact):
            continue
        if _is_name_like_handle(handle):
            kind = PIIKind.NAME
        elif _CUSTOMER_ROLES.match(role):
            # A platform user ID identifies the customer as surely as a name does.
            kind = PIIKind.ACCOUNT
        else:
            # Staff codes such as ``AU-0020`` identify an employee, not the client.
            continue
        yield Detection(
            kind, match.start("handle"), match.end("handle"), handle,
            "pattern.name.speaker" if kind is PIIKind.NAME else "pattern.account.speaker",
            Confidence.PATTERN,
        )


def _cue_names(text: str) -> Iterator[Detection]:
    """Names introduced by a greeting, a title or a self-introduction."""
    for match in _SPELLED_NAME.finditer(text):
        value = match.group().rstrip(".")
        if sum(character.isalpha() for character in value) >= 4:
            yield Detection(
                PIIKind.NAME, match.start(), match.start() + len(value), value,
                "pattern.name.spelled", Confidence.PATTERN,
            )
    for label, pattern in (
        ("salutation", _SALUTATION), ("title", _TITLED), ("intro", _SELF_INTRO),
        ("label", _NAME_LABEL), ("alias", _ALIAS), ("role", _ROLE_CUE),
        ("alias", _BRACKET_ALIAS), ("salutation", _INITIALS_SALUTATION),
    ):
        for match in pattern.finditer(text):
            words = match.group("name").split()
            if not _is_name_word(words[0]):
                continue
            if len(words) > 1 and not _is_name_word(words[1]):
                words = words[:1]
            start = match.start("name")
            end = start + len(" ".join(words)) if len(words) == 1 else match.end("name")
            yield Detection(
                PIIKind.NAME, start, end, text[start:end],
                f"pattern.name.{label}", Confidence.PATTERN,
            )


# The most common mainland surnames. A surname followed by an honorific is how
# Chinese service chats address a customer (王女士, 李先生), and jieba does not
# reliably tag the pair when it is glued to surrounding text.
ZH_SURNAMES = (
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘蒋蔡余杜叶"
    "程苏魏吕丁任沈姚卢姜崔钟谭陆汪范贾夏韦付邹孟熊秦邱尹薛闫段雷侯史陶黎贺顾毛郝龚"
    "邵钱严覃武戴莫孔汤温康施牛樊葛邢齐易乔伍庞颜倪庄聂章鲁岳翟殷詹申欧耿焦俞柳甘祝"
    "舒阮柯纪梅童凌毕裴霍涂苗谷盛翁冉骆蓝辛靳柴蒙鲍喻祁蒲滕屈饶牟艾尤穆卓缪简麦褚"
    "娄窦戚岑景宫费卜冷晏席卫米柏宗瞿桂佟臧闵苟邬卞姬栾隋刁巫寇桑郎甄丛仲虞敖巩佘"
    "池邝高金石方白于万江龙华常洪游左包古关路"
)
ZH_COMPOUND_SURNAMES = ("欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "令狐", "慕容", "尉迟",
                        "长孙", "宇文", "司徒", "夏侯", "公孙", "端木")
ZH_HONORIFICS = (
    "先生", "女士", "小姐", "太太", "夫人", "老师", "经理", "总监", "医生", "律师",
    "同学", "阿姨", "叔叔",
)
# Surname immediately followed by the honorific only. Allowing given-name characters
# in between made "谢谢王先生" match from the first 谢; jieba owns full names.
_ZH_HONORIFIC_NAME = re.compile(rf"[{ZH_SURNAMES}](?:{'|'.join(ZH_HONORIFICS)})")

# Mainland street addresses: administrative levels, a road, a number with 号, then
# optional building/floor/room. Deterministic, so it does not depend on jieba's tags
# (it has tagged the road name 红星路 as a person, breaking its address chain).
_ADDRESS_CN = re.compile(
    r"(?:[一-鿿]{2,7}?(?:省|自治区|特别行政区))?(?:[一-鿿]{1,7}?(?:市|区|县|镇|乡|街道)){1,3}"
    r"[一-鿿A-Za-z0-9]{1,12}?(?:大道|路|街|道|巷|弄|胡同|里)(?:[一二三四五六七八九十0-9]{1,3}段)?"
    r"[0-9一二三四五六七八九十百]+号(?:院|楼)?"
    r"(?:[一-鿿A-Za-z0-9#-]{0,16}?(?:室|楼|层|栋|座|单元|号楼))*"
    # A bare unit number after the building ("B座2206").
    r"(?:\d{1,5}(?:室|号)?)?"
)
# Lead-in words the lazy administrative segment can absorb ("我现住成都市...").
_CN_ADDRESS_LEAD = re.compile(
    r"^(?:我|您|他|她|们|的|现|目前|现在|家|住址|住|址|在|于|是|为|地址|位于|居住|搬到|搬去|寄到|寄往|送到|改为|改成)+"
)
# Informal forms of address: 老王 / 小李 (old/little + surname) and stated nicknames.
_ZH_INFORMAL_NAME = re.compile(
    rf"(?<![一-鿿])[老小][{ZH_SURNAMES}](?=[，。,.!！?？\s的说呀啊吧呢]|$)"
    r"|(?:小名(?:叫|是)?|昵称(?:是)?|外号(?:叫)?|叫我)[ \t:：]*"
    r"([一-鿿]{2,3}?)(?=[，。,.!！?？\s]|$)"
)

# People named after a role or relationship word ("丈夫林志远也会联系", "客户 郑海燕 (").
_ZH_RELATION_NAME = re.compile(
    r"(?:客户|丈夫|老公|先生|妻子|太太|老婆|妈妈|母亲|爸爸|父亲|儿子|女儿|哥哥|姐姐|弟弟|妹妹|"
    r"朋友|同事|助理|受益人|授权人|代理人|监护人|配偶|邻居|介绍人|推荐人)[ \t]*"
    rf"((?:{'|'.join(ZH_COMPOUND_SURNAMES)})[一-鿿]{{1,2}}?|[{ZH_SURNAMES}][一-鿿]{{1,2}}?)"
    r"(?=[^一-鿿]|也|说|称|会|的|已|要|在|是|和|与|将|于|女士|先生|$)"
)

# Referral and invitation codes are issued per person and trace straight back to them.
_REFERRAL_CODE = re.compile(
    r"(?:邀请码|推荐码|邀请代码|优惠码|referral|invit(?:e|ation)|promo|voucher|coupon)"
    # The code may come later in the sentence ("... is not working, the code is X").
    r"[^\n]{0,80}?"
    # Upper-case letters, digits and inner hyphens, containing a digit, 4+ characters;
    # this keeps "referral code from a friend" intact.
    r"(?<![A-Za-z0-9-])((?=[A-Z-]*\d)(?=[A-Z0-9-]{4})[A-Z0-9]+(?:-[A-Z0-9]+)*)(?![A-Za-z0-9-])"
)
# A bare 客户 ("customer") is not a name label: "客户通知函" is a letter title.
_NAME_LABELLED_ZH = re.compile(
    r"(?:客户姓名|姓名|户名|戶名)[ \t:：]*\n?[ \t]*"
    r"([一-鿿]{2,4}?)(?=已|确认|[，。；,.;（(\s]|$)"
    # Recipient/contact labels also introduce departments ("收件人：客服中心"), so the
    # value must start with a surname.
    rf"|(?:联系人|收件人|发件人)[ \t:：]*\n?[ \t]*"
    rf"([{ZH_SURNAMES}][一-鿿]{{1,3}}?)(?=[，。；,.;（(<\s]|$)"
    # "客户王芳已确认": the customer named directly before a status verb.
    rf"|客户([{ZH_SURNAMES}][一-鿿]{{1,3}}?)(?=已|确认)"
)
_ZH_SELF_INTRO = re.compile(
    rf"(?:我叫|我是|我的名字是|我的名字叫|名字是|名字叫|中文名(?:字)?(?:是|叫)?|本人)"
    rf"[ \t:：]*((?:{'|'.join(ZH_COMPOUND_SURNAMES)})[一-鿿]{{1,2}}|[{ZH_SURNAMES}][一-鿿]{{1,2}})"
)


# A short Han run inside English prose ("the customer Chen Yu (陈宇, WeChat ...)") is
# almost always a name; jieba segments it without context and often misses it.
_HAN_IN_LATIN = re.compile(r"(?<![一-鿿])[一-鿿]{2,4}(?![一-鿿])")
_NOT_HAN_NAMES = frozenset({"谢谢", "你好", "您好", "好的", "客服", "客户", "先生", "女士", "微信"})


def _han_names_in_latin(text: str) -> Iterator[Detection]:
    han = sum("一" <= character <= "鿿" for character in text)
    latin = sum(character.isascii() and character.isalpha() for character in text)
    if not han or latin < 2 * han:
        return
    for match in _HAN_IN_LATIN.finditer(text):
        value = match.group()
        starts_with_surname = value[0] in ZH_SURNAMES or value[:2] in ZH_COMPOUND_SURNAMES
        if starts_with_surname and value not in _NOT_HAN_NAMES and not is_organisation(value):
            yield Detection(
                PIIKind.NAME, match.start(), match.end(), value,
                "pattern.name.zh_in_latin", Confidence.PATTERN,
            )


def _contextual_names(text: str) -> Iterator[Detection]:
    for label, pattern in (("zh", _NAME_LABELLED_ZH), ("surname", _SURNAME_ACTION)):
        for match in pattern.finditer(text):
            group = next(index for index in range(1, pattern.groups + 1) if match.group(index))
            yield Detection(
                PIIKind.NAME, match.start(group), match.end(group), match.group(group),
                f"pattern.name.{label}", Confidence.PATTERN,
            )
    for match in _ZH_HONORIFIC_NAME.finditer(text):
        yield Detection(
            PIIKind.NAME, match.start(), match.end(), match.group(),
            "pattern.name.zh_honorific", Confidence.PATTERN,
        )
    for match in _ZH_RELATION_NAME.finditer(text):
        yield Detection(
            PIIKind.NAME, match.start(1), match.end(1), match.group(1),
            "pattern.name.zh_relation", Confidence.PATTERN,
        )
    for match in _ZH_INFORMAL_NAME.finditer(text):
        group = 1 if match.group(1) else 0
        yield Detection(
            PIIKind.NAME, match.start(group), match.end(group), match.group(group),
            "pattern.name.zh_informal", Confidence.PATTERN,
        )
    for match in _ZH_SELF_INTRO.finditer(text):
        yield Detection(
            PIIKind.NAME, match.start(1), match.end(1), match.group(1),
            "pattern.name.zh_intro", Confidence.PATTERN,
        )
    for match in _REFERRAL_CODE.finditer(text):
        yield Detection(
            PIIKind.ACCOUNT, match.start(1), match.end(1), match.group(1),
            "pattern.account.referral", Confidence.PATTERN,
        )
    yield from _speaker_handles(text)
    yield from _cue_names(text)
    yield from _han_names_in_latin(text)

# Account numbers have no intrinsic shape, so they are only detected where a label
# says what they are. Matching bare digit runs would flag every reference number in
# the document and make the output unreadable.
# A generic Reference label does not establish an account; require an account
# label or ACC prefix, preserving account-reference fields without flagging quantities.
_ACCOUNT_LABELLED = re.compile(
    r"(?:account|acct|a/c|账号|帳號|账户|帳戶|卡号|銀行卡|银行卡|户号)[ \t]*"
    r"(?:number|no\.?|ending|reference|ref\.?|尾号|尾號|号码|號碼|号|號)?"
    r"(?:[ \t]*(?:is|是|为|為))?"
    r"[\s:：#-]*([A-Z]{0,4}[-]?\d[\d\s-]{4,})",
    re.IGNORECASE,
)
# Messaging handles identify a person as directly as a phone number does.
_MESSAGING_APPS = (
    r"(?:微信号?|微信ID|wechat(?:[ \t]*id)?|weixin|\bQQ号?|line(?:[ \t]+id)?|whatsapp|telegram|"
    r"skype|kakao(?:talk)?|signal|instagram|insta|twitter|facebook|tiktok|抖音|小红书|微博)"
)
_MESSAGING_ID = re.compile(
    _MESSAGING_APPS + r"[ \t]*(?:(?:id|handle|号)[ \t]*)?(?:is|是|为)?[ \t]*[:：]?[ \t]*\n?[ \t]*"
    r"@?([A-Za-z][-_.A-Za-z0-9]{3,30}|\d{5,12})(?![-_A-Za-z0-9])",
    re.IGNORECASE,
)
# "I only use fatima_dds on Telegram": the handle before the app name. Requires a
# digit, underscore or dot so ordinary words ("me on WhatsApp") never match.
_MESSAGING_ID_BEFORE = re.compile(
    r"(?<![\w@.])((?=[A-Za-z0-9._]*[\d_.])[A-Za-z][A-Za-z0-9._]{3,30})[ \t]+(?:on|via|in)[ \t]+"
    + _MESSAGING_APPS,
    re.IGNORECASE,
)
# Generic usernames introduced by a cue. After a label colon/equals (Markdown bold
# allowed) any token is a value ("Username: dwhite"). Elsewhere ("the username is X",
# "user name mb9983", "Hi nikita.ivanov") the token must carry a digit, dot or underscore,
# or be joined CamelCase ("IslaSimmons"), so "the username is required", "user name
# field" and "Hi team" never match.
_USERNAME_CUE = (
    r"(?:user[ \t-]?name|user[ \t-]?id|log[ \t-]?in(?:[ \t]+(?:id|name))?|screen[ \t]+name|"
    r"account[ \t]+name|gamer[ \t-]?tag)s?"
)
_USERNAME_LABELLED = re.compile(
    rf"\b{_USERNAME_CUE}[*_ \t]*[:=][*_ \t]*[\"'“]?([A-Za-z0-9][-_.A-Za-z0-9]{{1,40}}[A-Za-z0-9])"
    r"(?![-_A-Za-z0-9@]|\.[A-Za-z0-9])",
    re.IGNORECASE,
)
_MARKED_HANDLE = (
    r"((?=[A-Za-z0-9._-]*[\d_.])[A-Za-z][A-Za-z0-9._-]{1,40}[A-Za-z0-9]"
    r"|(?-i:[A-Z][a-z]+[A-Z][a-z]+[A-Za-z]*\d*))"
    r"(?![-_A-Za-z0-9@]|\.[A-Za-z0-9])"
)
_USERNAME_STATED = re.compile(
    rf"\b{_USERNAME_CUE}\b[^.\n:]{{0,40}}?\b(?:is|was|of)[ \t]+[\"'“]?" + _MARKED_HANDLE,
    re.IGNORECASE,
)
_USERNAME_APPOSED = re.compile(
    rf"\b{_USERNAME_CUE}[ \t]*,?[ \t]+(?:such[ \t]+as[ \t]+|like[ \t]+)?[\"'“]?"
    + _MARKED_HANDLE,
    re.IGNORECASE,
)
_GREETING_HANDLE = re.compile(r"\b(?:hi|hello|hey|dear)[ \t]+" + _MARKED_HANDLE, re.IGNORECASE)
_USERNAME_STOPWORDS = frozenset({"none", "n/a", "na", "null", "required", "optional", "unknown"})

# Social handles written with a leading @, not part of an email address.
_AT_HANDLE = re.compile(r"(?<![\w.@])@[A-Za-z0-9_][A-Za-z0-9_.]{2,29}(?<!\.)(?![\w@])")
_BANK_ACCOUNT = re.compile(
    r"(?:\bbank\b|\bacc\b\.?|\bacct\b|\baccount\b|\bIBAN\b|\bSWIFT\b|开户行|银行|账号|帳號)"
    r"[^\n\d]{0,30}?(?<![\w-])(\d(?:[ -]?\d){5,24})(?![\d])",
    re.IGNORECASE,
)
_DATE_PREFIX = re.compile(r"(?:19|20)\d{2}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}-(?:19|20)\d{2}")
_WXID = re.compile(r"\bwxid_[a-z0-9]{4,30}\b", re.IGNORECASE)
_CUSTOMER_NUMBER = re.compile(
    r"(?:customer|client|member|user|broker|trading|holder|portfolio|客户|会员|用户)[ \t]*"
    r"(?:number|no\.?|id|code|#|号|编号|号码|代码)[ \t]*(?:is|是|为)?[ \t]*[:：#]?[ \t]*"
    r"([A-Z]{0,4}-?\d[\d \-]{3,}\d|[A-Z]{0,4}-?\d{4,})",
    re.IGNORECASE,
)
_LOOKS_LIKE_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}")
# Prefixes marking customer account numbers ("ACC-43021998"), plus any the organisation
# configures for its own (see Organisation). Staff codes (AU-0210) and tickets
# (TKT-806651) use other prefixes and stay readable.
ACCOUNT_PREFIXES = ("ACC", "ACCT")


def _account_prefixed(prefixes: Iterable[str]) -> re.Pattern[str]:
    alternatives = "|".join(sorted({re.escape(p) for p in prefixes}, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatives})[- ]?\d{{5,}}\b")
_BSB_LABELLED = re.compile(r"\bBSB[ \t:#-]*(\d{3}[ -]?\d{3})(?!\d)", re.IGNORECASE)
# Long bare numbers: account and card numbers given as a reply ("40518826") or in a
# table cell away from their label, and grouped card numbers ("6222 0212 3456 7890").
# NER-tier confidence so a phone, ID or date covering the same digits wins.
# Business references, versions and quantities are not personal identifiers.
_NON_PII_NUMBER_LABEL = re.compile(
    r"(?:reference|ref|ticket|case|order|version|ver|quantity|qty|amount|price|total|"
    r"balance|units|shares|volume|checksum|example|invoice|transaction|txn|trade|afsl|acl|"
    r"holder[ \t]+no|ticket[ \t]+no|"
    r"编号|订单|数量|金额|单号)\.?[ \t]*(?:no\.?|number|id|#)?[ \t]*[:：#]?[ \t]*$",
    re.IGNORECASE,
)
_LONG_NUMBER = re.compile(
    # 1300/1800/13xx numbers are businesses, not people.
    r"(?<![\w.,/:+-])(?!1[38]00[ -]?\d{3}[ -]?\d{3}(?!\d))"
    r"(?:\d{4}(?:[ -]\d{3,4}){2,4}|\d{8,19})(?![\d]|[.,]\d)"
)

# Australian street address with a required street number and street-type suffix.
# Requiring both avoids treating a bare suburb/city mention as identifying data.
_ADDRESS_AU = re.compile(
    # Not after a digit, colon or dot, so a clock time such as ``07:13:16`` cannot
    # supply the street number. The street line stays on one physical line and the
    # street type needs a word boundary: without both, ``16\nPlease`` read as
    # ``16 Pl`` (Place) and the replacement swallowed the line break and half a word.
    r"(?<![\w:.])(?:(?:unit|apt|apartment|suite|level|shop|flat)[ \t]+\d+[A-Z]?[ \t,/-]+)?"
    r"\d{1,5}[A-Z]?(?:/\d{1,5}[A-Z]?)?[ \t]+"
    r"(?:[A-Z][A-Z'’-]*[ \t]+){0,5}"
    r"(?:street|st|road|rd|avenue|ave|parade|pde|crescent|cres|lane|ln|"
    r"drive|dr|boulevard|blvd|highway|hwy|place|pl|court|ct|terrace|tce)\b"
    r"(?:,?\s+(?:[A-Z][A-Z'’-]*[ \t]+){1,4}(?:NSW|VIC|QLD|SA|WA|TAS|ACT|NT)[ \t]+\d{4}\b"
    r"|,[ \t]+[A-Z][A-Z'’-]*(?:[ \t]+[A-Z][A-Z'’-]*){0,3}(?=[.!?;\n]|$))?",
    re.IGNORECASE,
)
_AU_STATE = r"(?:NSW|VIC|QLD|SA|WA|TAS|ACT|NT)"
# Street types that are also ordinary words ("way", "view", "close"). The street name
# must be capitalised, so "2 accounts in a row" or "one more way" cannot match.
_ADDRESS_AU_CAPITALISED = re.compile(
    r"(?<![\w:.])(?:(?:Unit|Apt|Apartment|Suite|Level|Shop)[ \t]+\d+[A-Z]?[ \t,/-]+)?"
    r"\d{1,5}[A-Z]?(?:/\d{1,5}[A-Z]?)?[ \t]+(?:[A-Z][a-z'’-]+[ \t]+){1,4}"
    r"(?:Way|Grove|Gr|Close|Cl|Circuit|Cct|Esplanade|Esp|Square|Sq|Walk|Rise|Row|Loop|Mews|"
    r"Track|Promenade|Parkway|Gardens|Gdns|View|Crest|Ridge|Chase|Link|Bend|Glade|Green|"
    r"Heights|Hts|Vale|Quay|Wharf|Mall|Alley|Arcade|Boulevarde|Broadway|Cove|Point|Pt)\b"
    rf"(?:,?[ \t]+(?:[A-Z][A-Za-z'’-]*[ \t]+){{1,4}}{_AU_STATE}[ \t]+\d{{4}}\b)?"
)
# The locality line of a postal address block ("Box Hill VIC 3128"), which letters
# put on its own line below the street.
_ADDRESS_LOCALITY_AU = re.compile(
    rf"(?<![\w])(?:[A-Z][A-Za-z'’-]+[ \t]+){{1,3}}{_AU_STATE}[ \t]+\d{{4}}(?!\d)"
)
_POSTCODE_CN = re.compile(r"(?:邮编|邮政编码)[ \t:：]*(\d{6})(?!\d)")
_ADDRESS_PO_BOX_AU = re.compile(
    r"\bP\.?\s*O\.?\s+Box\s+\d+[A-Z]?,?\s+"
    r"(?:[A-Z][A-Z'’-]*\s+){1,4}(?:NSW|VIC|QLD|SA|WA|TAS|ACT|NT)\s+\d{4}\b",
    re.IGNORECASE,
)


def _has_dob_context(text: str, start: int, end: int) -> bool:
    """Distinguish birth dates from appointments and transaction dates."""
    # Dialogue transcripts often put the question and answer on adjacent lines.
    return _DOB_CONTEXT.search(text[max(0, start - 120) : min(len(text), end + 24)]) is not None


def _emails(text: str) -> Iterator[Detection]:
    for match in _EMAIL.finditer(text):
        yield Detection(
            kind=PIIKind.EMAIL,
            start=match.start(),
            end=match.end(),
            text=match.group(),
            detector="pattern.email",
            confidence=Confidence.VALIDATED,
        )


# Any number written with a leading + and country code is a phone number.
_PHONE_INTERNATIONAL = re.compile(
    r"(?<![\w+])\+[1-9]\d{0,2}[ -]?\(?\d{1,4}\)?(?:[ -]?\d){5,10}(?!\d)"
)


def _phones(text: str) -> Iterator[Detection]:
    for name, pattern in (("au", _PHONE_AU), ("cn", _PHONE_CN), ("intl", _PHONE_INTERNATIONAL)):
        for match in pattern.finditer(text):
            yield Detection(
                kind=PIIKind.PHONE,
                start=match.start(),
                end=match.end(),
                text=match.group(),
                detector=f"pattern.phone.{name}",
                confidence=Confidence.PATTERN,
            )


def _dob(text: str, match: re.Match[str], year: int, readings, detector: str):
    """Yield a DOB if the date is real and old enough to be a birth date.

    Dates at least _UNCUED_BIRTH_AGE years back are treated as birth dates without a
    cue: in customer-service material they almost always are, and chats often give
    the date as a bare reply to a question several messages earlier. Recent dates
    need a birth cue nearby so trade and appointment dates stay readable.
    """
    if not any(_date_is_real(year, month, day) for month, day in readings):
        return
    age = date.today().year - year
    cued = _has_dob_context(text, match.start(), match.end())
    if not cued and (
        age < _UNCUED_BIRTH_AGE
        or _NON_BIRTH_CONTEXT.search(text[max(0, match.start() - 40) : match.start()])
    ):
        return
    if age < _MIN_BIRTH_AGE:
        return
    yield Detection(
        PIIKind.DOB, match.start(), match.end(), match.group(), detector,
        Confidence.VALIDATED if cued else Confidence.PATTERN,
    )


def _dates(text: str) -> Iterator[Detection]:
    for match in _DOB_SLASH.finditer(text):
        first, second, year = (int(part) for part in match.groups())
        # Day-first and month-first both appear in these documents. Accept the
        # match if either reading is a real date, rather than picking one locale.
        yield from _dob(text, match, year, ((second, first), (first, second)), "pattern.dob.slash")
    for match in _DOB_ISO.finditer(text):
        year, month, day = (int(part) for part in match.groups())
        yield from _dob(text, match, year, ((month, day),), "pattern.dob.iso")
    for match in _DOB_HAN.finditer(text):
        year, month, day = (int(part) for part in match.groups())
        yield from _dob(text, match, year, ((month, day),), "pattern.dob.han")
    for match in _DOB_SHORT_YEAR.finditer(text):
        # Two-digit years are only trusted next to an explicit birth cue.
        if not _has_dob_context(text, match.start(), match.end()):
            continue
        day, middle, short_year = match.groups()
        month = int(middle) if middle.isdigit() else _MONTH_NUMBER[middle.casefold()[:3]]
        year = 1900 + int(short_year)
        if year < date.today().year - 100 + 1:
            year += 100
        yield from _dob(
            text, match, year, ((month, int(day)), (int(day), month)), "pattern.dob.short_year"
        )
    for match in _DOB_ORDINAL.finditer(text):
        tens, word, month_name, year = match.groups()
        day = _ORDINAL_WORDS[word.casefold()] + (
            {"twenty": 20, "thirty": 30}[tens.casefold()[:-1]] if tens else 0
        )
        month = _MONTH_NUMBER[month_name.casefold()[:3]]
        yield from _dob(text, match, int(year), ((month, day),), "pattern.dob.written")
    for pattern, order in ((_DOB_WRITTEN_DMY, "dmy"), (_DOB_WRITTEN_MDY, "mdy")):
        for match in pattern.finditer(text):
            parts = match.groups()
            day, month_name = (parts[0], parts[1]) if order == "dmy" else (parts[1], parts[0])
            month = _MONTH_NUMBER[month_name.casefold()[:3]]
            yield from _dob(
                text, match, int(parts[2]), ((month, int(day)),), "pattern.dob.written"
            )


def _id_numbers(text: str) -> Iterator[Detection]:
    for match in _ID_VALUE.finditer(text):
        # "2026-08-03 14" at the start of a chat timestamp is not an identifier.
        if _DATE_PREFIX.match(match.group()):
            continue
        digits = "".join(character for character in match.group() if character.isdigit())
        before = text[max(0, match.start() - _ID_WINDOW) : match.start()]
        # Checksummed Australian numbers are identifying even without a label.
        if (_tfn_is_valid(digits) and not match.group().isdigit()) or (
            _medicare_is_valid(digits) and " " in match.group()
        ):
            confidence = Confidence.VALIDATED
        elif _NOT_ID_LABEL.search(before):
            continue
        elif _ID_LABEL.search(before) or _ID_LABEL.search(text[match.end() : match.end() + 20]):
            # Label before the value, or just after it ("N4482019 (AU passport)").
            confidence = Confidence.PATTERN
        else:
            continue
        yield Detection(
            PIIKind.ID_NUMBER, match.start(), match.end(), match.group(),
            "pattern.id.labelled", confidence,
        )
    for match in _HKID.finditer(text):
        yield Detection(
            PIIKind.ID_NUMBER, match.start(), match.end(), match.group(),
            "pattern.id.hk", Confidence.VALIDATED,
        )
    for match in _CN_ID.finditer(text):
        if _cn_id_is_valid(match.group()):
            yield Detection(
                kind=PIIKind.ID_NUMBER,
                start=match.start(),
                end=match.end(),
                text=match.group(),
                detector="pattern.id.cn",
                confidence=Confidence.VALIDATED,
            )


def _accounts(text: str) -> Iterator[Detection]:
    for match in _ACCOUNT_LABELLED.finditer(text):
        # Report the number itself, not the label that introduced it. The pattern's
        # trailing [\d\s-] class greedily eats whitespace, including a newline, so
        # strip every trailing space and separator: leaving one in would push the
        # detection span over the following line break (the replacement would then
        # delete it) and, once replaced, stop the dummy from being recognised on the
        # leak-check rescan.
        value = match.group(1).rstrip(" \t\r\n-")
        if not value or _LOOKS_LIKE_DATE.fullmatch(value):
            continue
        start = match.start(1)
        yield Detection(
            kind=PIIKind.ACCOUNT,
            start=start,
            end=start + len(value),
            text=value,
            detector="pattern.account.labelled",
            confidence=Confidence.PATTERN,
        )
    for pattern, name in ((_MESSAGING_ID, "messaging"), (_CUSTOMER_NUMBER, "customer")):
        for match in pattern.finditer(text):
            yield Detection(
                PIIKind.ACCOUNT, match.start(1), match.end(1), match.group(1),
                f"pattern.account.{name}", Confidence.PATTERN,
            )
    for match in _MESSAGING_ID_BEFORE.finditer(text):
        yield Detection(
            PIIKind.ACCOUNT, match.start(1), match.end(1), match.group(1),
            "pattern.account.messaging", Confidence.PATTERN,
        )
    for pattern in (
        _USERNAME_LABELLED, _USERNAME_STATED, _USERNAME_APPOSED, _GREETING_HANDLE,
    ):
        for match in pattern.finditer(text):
            if match.group(1).casefold() in _USERNAME_STOPWORDS:
                continue
            yield Detection(
                PIIKind.ACCOUNT, match.start(1), match.end(1), match.group(1),
                "pattern.account.username", Confidence.PATTERN,
            )
    for match in _AT_HANDLE.finditer(text):
        yield Detection(
            PIIKind.ACCOUNT, match.start(), match.end(), match.group(),
            "pattern.account.handle", Confidence.PATTERN,
        )
    for match in _BANK_ACCOUNT.finditer(text):
        if _NON_PII_NUMBER_LABEL.search(
            text[max(0, match.start(1) - 30) : match.start(1)]
        ) or _DATE_PREFIX.match(match.group(1)):
            continue
        yield Detection(
            PIIKind.ACCOUNT, match.start(1), match.end(1), match.group(1),
            "pattern.account.bank", Confidence.PATTERN,
        )
    for match in _WXID.finditer(text):
        yield Detection(
            PIIKind.ACCOUNT, match.start(), match.end(), match.group(),
            "pattern.account.wxid", Confidence.VALIDATED,
        )
    for match in _BSB_LABELLED.finditer(text):
        yield Detection(
            PIIKind.ACCOUNT, match.start(1), match.end(1), match.group(1),
            "pattern.account.bsb", Confidence.PATTERN,
        )
    for match in _LONG_NUMBER.finditer(text):
        if _NON_PII_NUMBER_LABEL.search(text[max(0, match.start() - 30) : match.start()]):
            continue
        yield Detection(
            PIIKind.ACCOUNT, match.start(), match.end(), match.group(),
            "pattern.account.long_number", Confidence.NER,
        )
    for match in _ORGANISATION.get().account_prefixed.finditer(text):
        yield Detection(
            kind=PIIKind.ACCOUNT,
            start=match.start(),
            end=match.end(),
            text=match.group(),
            detector="pattern.account.prefixed",
            confidence=Confidence.PATTERN,
        )


# Mainland and Hong Kong addresses written in English: comma-separated capitalised
# segments ending in a province, municipality or HK region (optionally country and a
# six-digit postcode), e.g. "Room 1502, Building 7, Jinjiang District, Chengdu,
# Sichuan 610021" or "Flat B, 23/F, Tower 5, Taikoo Shing, Quarry Bay, Hong Kong". A
# list of places ("Sydney, Melbourne, Hong Kong") is not an address, so a unit,
# building, street or district word must appear somewhere in it.
_CN_EN_REGIONS = (
    "Anhui|Beijing|Chongqing|Fujian|Gansu|Guangdong|Guangxi|Guizhou|Hainan|Hebei|"
    "Heilongjiang|Henan|Hubei|Hunan|Inner Mongolia|Jiangsu|Jiangxi|Jilin|Liaoning|"
    "Ningxia|Qinghai|Shaanxi|Shandong|Shanghai|Shanxi|Sichuan|Tianjin|Tibet|Xinjiang|"
    "Yunnan|Zhejiang|Guangzhou|Shenzhen|Chengdu|Hangzhou|Wuhan|Nanjing|Suzhou|Xiamen|"
    "Qingdao|Dalian|Shenyang|Harbin|Changsha|Kunming|Zhengzhou|Jinan|Hefei|Fuzhou|"
    "Ningbo|Dongguan|Foshan|Wuxi|Hong[ ]Kong|Kowloon|New[ ]Territories|Macau|Macao"
)
_EN_SEGMENT = r"[A-Z0-9][A-Za-z0-9/#'.-]*(?:[ ][A-Z0-9][A-Za-z0-9/#'.-]*){0,5}"
_ADDRESS_CN_EN = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_EN_SEGMENT},[ ]?){{2,7}}"
    rf"(?:{_CN_EN_REGIONS})(?:[ ](?:Province|SAR|Municipality))?"
    r"(?:,?[ ](?:China|PRC|P\.R\.[ ]?China|Hong[ ]Kong|HK))?(?:,?[ ]\d{6})?"
    r"(?:,?[ ](?:China|PRC))?(?![A-Za-z0-9])"
)
_ADDRESS_CN_EN_KEYWORD = re.compile(
    r"\b(?:Room|Rm|Flat|Unit|Suite|Apt|Building|Bldg|Block|Blk|Tower|Floor|Road|Rd|Street|"
    r"St|Avenue|Ave|Lu|Jie|Dao|Lane|District|Dist|Estate|Court|Garden|Gardens|Village|"
    r"Mansion|Plaza|Community|Residence|No)\b|\d+/F\b"
)


def _addresses(text: str) -> Iterator[Detection]:
    for match in _POSTCODE_CN.finditer(text):
        yield Detection(
            PIIKind.ADDRESS, match.start(1), match.end(1), match.group(1),
            "pattern.address.postcode.cn", Confidence.PATTERN,
        )
    for match in _ADDRESS_CN.finditer(text):
        lead = _CN_ADDRESS_LEAD.match(match.group())
        start = match.start() + (lead.end() if lead else 0)
        if match.end() - start >= 6:
            yield Detection(
                PIIKind.ADDRESS, start, match.end(), text[start : match.end()],
                "pattern.address.cn", Confidence.PATTERN,
            )
    for match in _ADDRESS_CN_EN.finditer(text):
        if _ADDRESS_CN_EN_KEYWORD.search(match.group()):
            yield Detection(
                PIIKind.ADDRESS, match.start(), match.end(), match.group(),
                "pattern.address.cn_en", Confidence.PATTERN,
            )
    for detector_name, pattern in (
        ("pattern.address.au", _ADDRESS_AU),
        ("pattern.address.po_box.au", _ADDRESS_PO_BOX_AU),
        ("pattern.address.au", _ADDRESS_AU_CAPITALISED),
        ("pattern.address.locality.au", _ADDRESS_LOCALITY_AU),
    ):
        for match in pattern.finditer(text):
            yield Detection(
                kind=PIIKind.ADDRESS,
                start=match.start(),
                end=match.end(),
                text=match.group(),
                detector=detector_name,
                confidence=Confidence.PATTERN,
            )


def detect_patterns(text: str) -> list[Detection]:
    """Run every pattern detector over `text`."""
    search_text = to_search_text(text)
    found: list[Detection] = []
    for detector in (
        _emails, _phones, _dates, _id_numbers, _accounts, _addresses, _contextual_names,
    ):
        found.extend(detector(search_text))
    # Carry the original characters, not the folded ones, so replacement reporting
    # and pseudonym keys reflect what was actually in the document.
    return [
        Detection(
            kind=item.kind,
            start=item.start,
            end=item.end,
            text=text[item.start : item.end],
            detector=item.detector,
            confidence=item.confidence,
        )
        for item in found
    ]


# Local Chinese NER for names and addresses deterministic patterns miss.
# This is the least certain layer, a segmenter's guess rather than a match or a
# checksum, so everything it finds is Confidence.NER and loses every overlap to the
# layers above. Flags come from jieba's part-of-speech tags: 'nr' is a personal name,
# 'ns' a place name, and the rest are the ordinary nouns, numbers and quantifiers a
# street address is spelled out of.
_ADDRESS_BRIDGE = frozenset({"ns", "n", "m", "q", "nz", "b"})
# A street-level suffix. Its presence is what separates a real address from a bare
# city or province mention, which is not worth redacting and is noisy to flag.
_FINE_SUFFIX = frozenset("号室楼层路街道巷弄村组栋幢座坊苑里")


def _has_han(text: str) -> bool:
    return any("一" <= character <= "鿿" for character in text)


def detect_ner(text: str) -> list[Detection]:
    """Find Chinese names and street addresses by word segmentation.

    Runs only on text containing Han characters. This layer targets Chinese names
    and addresses, and segmenting English would load a heavy dictionary and
    invent false positives for no gain. Patterns already cover the language-agnostic
    kinds (phone, email, dates, IDs) for both languages.
    """
    if not _has_han(text):
        return []
    # Imported here, not at module load: jieba builds a prefix dictionary on first
    # use, about a second of work and several MB, which an English-only run never pays.
    if "jieba" not in sys.modules:
        # jieba 0.42.1 reads its dictionaries through pkg_resources whenever that imports.
        # The pinned setuptools no longer ships it, and a stale stub left by an upgrade
        # (macOS CI, Python 3.11) imports without resource_stream: force jieba's open().
        saved = sys.modules.get("pkg_resources")
        sys.modules["pkg_resources"] = None
        try:
            import jieba.posseg
        finally:
            if saved is None:
                del sys.modules["pkg_resources"]
            else:
                sys.modules["pkg_resources"] = saved
    import jieba
    import jieba.posseg as pseg

    # Silence jieba's "Building prefix dict" chatter, which otherwise lands on the
    # operator's console mid-run and reads like a fault. 60 is above CRITICAL.
    jieba.setLogLevel(60)

    # Segment the folded search text, so full-width digits in an address are ASCII by
    # the time jieba sees them. It is length preserving, so an offset into it is also
    # an offset into the original, and text[start:end] recovers the real characters.
    search = to_search_text(text)
    tokens = list(pseg.cut(search))

    # jieba's tokens are contiguous and cover the input exactly, so a running sum of
    # their lengths is each token's start offset.
    offsets: list[int] = []
    cursor = 0
    for token in tokens:
        offsets.append(cursor)
        cursor += len(token.word)

    found: list[Detection] = []

    # Names. jieba often splits a full name into a surname and a given name, both
    # tagged 'nr' ("陈" + "秀英"), so adjacent 'nr' tokens are merged first. A single
    # character is almost always a surname fragment or noise, so require two; and it
    # tags plenty of ordinary words 'nr' ("通知函", notice letter), so a real name must
    # start with a surname.
    index = 0
    while index < len(tokens):
        if tokens[index].flag != "nr":
            index += 1
            continue
        start = offsets[index]
        following = index + 1
        while following < len(tokens) and tokens[following].flag == "nr":
            following += 1
        end = offsets[following - 1] + len(tokens[following - 1].word)
        word = text[start:end]
        if (
            len(word) >= 2
            and (word[0] in ZH_SURNAMES or word[:2] in ZH_COMPOUND_SURNAMES)
        ):
            found.append(
                Detection(
                    kind=PIIKind.NAME, start=start, end=end, text=word,
                    detector="ner.name", confidence=Confidence.NER,
                )
            )
        index = following

    # Addresses. Anchor on a place name and absorb the trailing street, number and
    # building tokens jieba leaves separate, then keep the run only if it reached a
    # street-level suffix.
    index = 0
    while index < len(tokens):
        if tokens[index].flag != "ns":
            index += 1
            continue
        start = offsets[index]
        end = offsets[index] + len(tokens[index].word)
        following = index + 1
        while following < len(tokens) and tokens[following].flag in _ADDRESS_BRIDGE:
            end = offsets[following] + len(tokens[following].word)
            following += 1
        run = text[start:end]
        if any(character in _FINE_SUFFIX for character in run):
            found.append(
                Detection(
                    kind=PIIKind.ADDRESS,
                    start=start,
                    end=end,
                    text=run,
                    detector="ner.address",
                    confidence=Confidence.NER,
                )
            )
        index = following

    return found


_BUSINESS_WORDS = frozenset(
    "trader trade trading brokers broker onboarding team support au australia hk nz us "
    "securities international app ai desk compliance operations group pty ltd limited "
    "service services account accounts markets online platform help centre center "
    "client clients customer care wealth fund funds holdings global".split()
)


_HYPHEN_TAIL = re.compile(r"-[A-Za-z]+")


def _with_hyphenated_tail(text: str, item: Detection) -> Detection:
    """Extend "Hyun" to "Hyun-woo": a hyphenated given name must go as one."""
    tail = _HYPHEN_TAIL.match(text, item.end)
    if not tail or not item.text[-1:].isalpha():
        return item
    return replace(item, end=tail.end(), text=text[item.start : tail.end()])


def _fold_term(value: str) -> str:
    return " ".join(value.casefold().split()).strip(" .,'’")


def _term_words(folded: str) -> list[str]:
    return [word for word in re.split(r"[\s（）()·,-]+", folded) if word]


@dataclass(frozen=True)
class Organisation:
    """What the organisation whose files are sanitised tells Panda about itself.

    Keep terms are names that are never personal data: the organisation's own, its
    products and bots. A NER model tags many brand names as people (an animal or a
    surname used as a brand), and batch-wide propagation would then erase the brand
    from every document. Each term is kept exactly; its distinctive words ("acme" in
    "Acme Brokers") are also kept when they appear with business words only ("Acme
    Trader", "ACME ONBOARDING"), but not next to a name ("Acme Zhang"). A Chinese term
    also keeps anything that starts with it ("白鹤证券（澳大利亚）").

    Account prefixes mark the organisation's customer numbers ("CB-43021998"), which
    are then replaced even where no label says they are account numbers.
    """

    exact: frozenset[str] = frozenset()
    brands: frozenset[str] = frozenset()
    han_prefixes: tuple[str, ...] = field(default=())
    account_prefixed: re.Pattern[str] = field(
        default=_account_prefixed(ACCOUNT_PREFIXES), compare=False
    )

    @classmethod
    def build(cls, keep: Iterable[str] = (), account_prefixes: Iterable[str] = ()) -> Organisation:
        exact, brands, han = set(), set(), []
        for term in keep:
            folded = _fold_term(term)
            if not folded:
                continue
            exact.add(folded)
            if _has_han(folded):
                han.append(folded)
            brands.update(
                word for word in _term_words(folded)
                if word not in _BUSINESS_WORDS and not _has_han(word)
            )
        prefixes = [*ACCOUNT_PREFIXES, *(p.strip().rstrip("-") for p in account_prefixes)]
        return cls(
            frozenset(exact), frozenset(brands), tuple(han),
            _account_prefixed(p for p in prefixes if p),
        )


_ORGANISATION: ContextVar[Organisation] = ContextVar(
    "panda_organisation", default=Organisation()
)


@contextmanager
def organisation(
    keep: Iterable[str] = (), account_prefixes: Iterable[str] = ()
) -> Iterator[Organisation]:
    """Apply an organisation's keep terms and account prefixes inside the block."""
    token = _ORGANISATION.set(Organisation.build(keep, account_prefixes))
    try:
        yield _ORGANISATION.get()
    finally:
        _ORGANISATION.reset(token)


def is_organisation(value: str) -> bool:
    """The configured organisation names, including product and team names built on them."""
    keep = _ORGANISATION.get()
    folded = _fold_term(value)
    if folded in keep.exact or folded.startswith(keep.han_prefixes):
        return True
    words = _term_words(folded)
    return (
        any(word in keep.brands for word in words)
        and all(word in keep.brands or word in _BUSINESS_WORDS for word in words)
    )


def resolve_overlaps(detections: Iterable[Detection]) -> list[Detection]:
    """Merge overlapping detections into one replacement covering all of them.

    Overlaps are normal rather than exceptional: a name can sit inside an email
    address, a Chinese ID contains a date, and a propagated given name (建国) can sit
    inside a street name (建国路). Keeping only the most trusted detection used to
    replace the fragment and leave the rest of the container behind, so the whole
    overlapping cluster is now replaced. The most trusted member (confidence, then
    length, then position) still decides the kind, detector and surrogate.
    """
    ordered = sorted(detections, key=lambda item: (item.start, -item.end))
    merged: list[Detection] = []
    cluster: list[Detection] = []

    def flush() -> None:
        if not cluster:
            return
        best = min(cluster, key=lambda item: (-item.confidence, -item.length, item.start))
        start = cluster[0].start
        end = max(item.end for item in cluster)
        if (best.start, best.end) == (start, end):
            merged.append(best)
        else:
            # Rebuild the covered text from the members: together they span it exactly.
            pieces, cursor = [], start
            for item in cluster:
                if item.end > cursor:
                    pieces.append(item.text[cursor - item.start :])
                    cursor = item.end
            merged.append(replace(best, start=start, end=end, text="".join(pieces)))
        cluster.clear()

    for detection in ordered:
        if cluster and detection.start >= max(item.end for item in cluster):
            flush()
        cluster.append(detection)
    flush()
    return merged


@dataclass(frozen=True)
class DetectorEngine:
    """One job's configured recognisers.

    Format handlers receive the same engine instance throughout a job. Keeping
    orchestration here gives future local NLP providers one place to load models,
    route languages, apply thresholds, and report their configuration.
    The engine is passed through every format handler so expensive NLP providers
    load once and configuration stays identical throughout a job.
    """

    providers: tuple[RecognizerProvider, ...] = ()

    def prepare(self) -> None:
        """Load configured local providers before processing any client document."""
        for provider in self.providers:
            provider.prepare()

    def detect(self, text: str) -> list[Detection]:
        check_text(text)
        detections: list[Detection] = []
        detections.extend(detect_patterns(text))
        detections.extend(detect_ner(text))
        for provider in self.providers:
            detections.extend(provider.detect(text))
        kept = []
        for item in detections:
            if item.kind is PIIKind.NAME:
                if is_organisation(item.text) or not any(
                    _is_name_word(word) for word in re.findall(r"[A-Za-z]+", item.text)
                ) and not _has_han(item.text):
                    continue
                item = _with_hyphenated_tail(text, item)
            kept.append(item)
        return resolve_overlaps(kept)

    def with_provider(self, provider: RecognizerProvider) -> DetectorEngine:
        """Return an engine sharing this one's (already loaded) providers plus one more."""
        return DetectorEngine(providers=(*self.providers, provider))

    @property
    def identity(self) -> tuple[str, ...]:
        """Safe recogniser identities suitable for audit metadata."""
        return ("patterns.au-cn.v2", "jieba.zh.v1") + tuple(
            provider.identity for provider in self.providers
        )


class RecognizerProvider(Protocol):
    """A local probabilistic recogniser plugged into a detector engine."""

    @property
    def identity(self) -> str: ...

    def detect(self, text: str) -> list[Detection]: ...

    def prepare(self) -> None: ...


def default_detector_engine() -> DetectorEngine:
    """Build the production recogniser set once for one CLI job."""
    from panda.spacy_recognizer import SpacyRecognizer

    return DetectorEngine(providers=(SpacyRecognizer(),))
