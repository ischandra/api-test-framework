"""Seeded synthetic data for request payloads.

Resolves ``{{$name}}`` / ``{{$name(args)}}`` placeholders. It supports the common
Postman dynamic variables (``$guid``, ``$randomEmail``, ``$randomInt``, ...), so
imported collections work unchanged, plus parametrised helpers for test design
(``$int(1,10)``, ``$oneOf(A,B)``, ``$pattern(ORD-###)``, ``$string(256)``, ...)
and hostile values for negative tests (``$sqlInjection``, ``$xss``, ...).

All contact data is fictional: example.* domains, 555 phone numbers, RFC 5737 IPs,
public gateway test card numbers.
"""
from __future__ import annotations

import datetime as _dt
import itertools
import random
import re
import string
import time
import uuid
from typing import Any, Callable, Dict, List

from faker import Faker

_SAFE_DOMAINS = ["example.com", "example.org", "example.net"]
_TEST_CARDS = [
    "4111111111111111", "4242424242424242", "4012888888881881", "5555555555554444",
    "5105105105105100", "378282246310005", "6011111111111117",
]
_DOC_NETS = ["192.0.2.", "198.51.100.", "203.0.113."]

EDGE_STRINGS = [
    "", " ", "  padded  ", "x" * 256, "y" * 4096, "O'Brien", "' OR '1'='1",
    "Robert'); DROP TABLE users;--", "<script>alert(1)</script>", "Zoë Ångström",
    "李小龙", "مرحبا", "😀🚀", "line1\nline2", "null", "\\", "%00", "../../etc/passwd",
]

_CALL = re.compile(r"^\$([A-Za-z_][\w]*)\s*(?:\((.*)\))?$", re.S)


def _base36(n: int, width: int) -> str:
    digits = string.digits + string.ascii_lowercase
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out.rjust(width, "0")[-width:]


class UnknownDynamicVariable(ValueError):
    pass


class DataGen:
    """Synthetic data source. The same seed always produces the same sequence."""

    def __init__(self, seed: int | None = None):
        self.seed = seed if seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
        self.rng = random.Random(self.seed)
        self.fake = Faker()
        self.fake.seed_instance(self.seed)
        # Not seeded on purpose: differs every run so values with uniqueness constraints
        # (emails, usernames, codes) never collide with data left by earlier runs.
        self.run_id = _base36(int(time.time() * 1000) % 36**6 * 1296 + random.SystemRandom().randrange(1296), 8)
        self._counter = itertools.count(1)
        self._fns: Dict[str, Callable[..., Any]] = self._registry()

    # ------------------------------------------------------------------ API

    def resolve(self, expr: str) -> Any:
        """Evaluates ``$name`` or ``$name(arg, ...)``."""
        m = _CALL.match(expr.strip())
        if not m:
            raise UnknownDynamicVariable(f"Malformed dynamic variable '{expr}'")
        name, raw_args = m.group(1), m.group(2)
        fn = self._fns.get(name)
        if fn is None:
            raise UnknownDynamicVariable(
                f"Unknown dynamic variable '${name}'. Known: {', '.join('$' + n for n in sorted(self._fns))}")
        args = [a.strip() for a in raw_args.split(",")] if raw_args and raw_args.strip() else []
        try:
            return fn(*args)
        except TypeError as e:
            raise UnknownDynamicVariable(f"Bad arguments for '{expr}': {e}") from e

    def names(self) -> List[str]:
        return sorted(self._fns)

    # Convenience methods for code-based tests -------------------------------

    def email(self, domain: str | None = None) -> str:
        return self._email(domain)

    def unique(self, prefix: str = "") -> str:
        """Run-unique token, e.g. ``k3x9q2ab-7``."""
        return f"{prefix}{self.run_id}-{next(self._counter)}"

    def unique_email(self, domain: str | None = None) -> str:
        return self._email(domain, unique=True)

    def person(self, unique: bool = True) -> Dict[str, Any]:
        """Name/email/phone. ``unique=True`` makes the email unique across runs."""
        first, last = self.fake.first_name(), self.fake.last_name()
        return {"firstName": first, "lastName": last, "name": f"{first} {last}",
                "email": self._email(None, first, last, unique=unique), "phone": self._phone()}

    def int(self, lo: int = 0, hi: int = 1000) -> int:
        return self.rng.randint(lo, hi)

    def pattern(self, pattern: str) -> str:
        return self._pattern(pattern)

    # ------------------------------------------------------------ internals

    def _email(self, domain: str | None = None, first: str | None = None, last: str | None = None,
               unique: bool = False) -> str:
        first = first or self.fake.first_name()
        last = last or self.fake.last_name()
        local = re.sub(r"[^a-z0-9]", "", first.lower()) + "." + re.sub(r"[^a-z0-9]", "", last.lower())
        if unique:
            local += "." + self.unique()
        elif self.rng.random() < 0.6:
            local += str(self.rng.randint(1, 9999))
        return f"{local}@{domain or self.rng.choice(_SAFE_DOMAINS)}"

    def _phone(self) -> str:
        return f"+1-{self.rng.randint(201, 989):03d}-555-{self.rng.randint(100, 199):04d}"

    def _uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def _pattern(self, pattern: str) -> str:
        out, i = [], 0
        alnum = string.ascii_uppercase + string.digits
        while i < len(pattern):
            c = pattern[i]
            if c == "\\" and i + 1 < len(pattern):
                i += 1
                out.append(pattern[i])
            elif c == "#":
                out.append(self.rng.choice(string.digits))
            elif c == "?":
                out.append(self.rng.choice(string.ascii_uppercase))
            elif c == "*":
                out.append(self.rng.choice(alnum))
            else:
                out.append(c)
            i += 1
        return "".join(out)

    def _string(self, length: str = "10", charset: str = "alnum") -> str:
        pools = {"alnum": string.ascii_letters + string.digits, "alpha": string.ascii_letters,
                 "digits": string.digits, "hex": "0123456789abcdef", "unicode": "aéßøДжλ中文😀"}
        pool = pools.get(charset, charset)
        return "".join(self.rng.choice(pool) for _ in range(int(length)))

    def _date(self, from_days: str = "-365", to_days: str = "0") -> str:
        today = _dt.date.today()
        return (today + _dt.timedelta(days=self.rng.randint(int(from_days), int(to_days)))).isoformat()

    def _registry(self) -> Dict[str, Callable[..., Any]]:
        f, r = self.fake, self.rng
        now = lambda: _dt.datetime.now(_dt.timezone.utc)  # noqa: E731
        return {
            # --- Postman-compatible dynamic variables ---
            "guid": self._uuid,
            "randomUUID": self._uuid,
            "timestamp": lambda: int(time.time()),
            "isoTimestamp": lambda: now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "randomInt": lambda: r.randint(0, 1000),
            "randomBoolean": lambda: r.random() < 0.5,
            "randomFirstName": f.first_name,
            "randomLastName": f.last_name,
            "randomFullName": lambda: f"{f.first_name()} {f.last_name()}",
            "randomNamePrefix": f.prefix,
            "randomUserName": f.user_name,
            "randomEmail": lambda: self._email(),
            "randomExampleEmail": lambda: self._email(),
            "randomPassword": lambda: f.password(length=12),
            "randomPhoneNumber": self._phone,
            "randomStreetAddress": f.street_address,
            "randomCity": f.city,
            "randomCountry": f.country,
            "randomCountryCode": f.country_code,
            "randomZipCode": lambda: f"{r.randint(1001, 99950):05d}",
            "randomLatitude": lambda: round(r.uniform(-90, 90), 6),
            "randomLongitude": lambda: round(r.uniform(-180, 180), 6),
            "randomCompanyName": f.company,
            "randomJobTitle": f.job,
            "randomDepartment": lambda: r.choice(["Engineering", "Sales", "Finance", "HR", "Support", "Marketing"]),
            "randomProductName": lambda: f"{f.color_name()} {f.word().title()}",
            "randomPrice": lambda: round(r.uniform(1, 1000), 2),
            "randomCurrencyCode": f.currency_code,
            "randomCreditCardNumber": lambda: r.choice(_TEST_CARDS),
            "randomBankAccount": lambda: f"{r.randint(10000000, 99999999)}",
            "randomColor": f.color_name,
            "randomWord": f.word,
            "randomWords": lambda: " ".join(f.words(3)),
            "randomLoremSentence": f.sentence,
            "randomLoremParagraph": f.paragraph,
            "randomAlphaNumeric": lambda: r.choice(string.ascii_lowercase + string.digits),
            "randomDomainName": lambda: f"{f.word()}.{r.choice(_SAFE_DOMAINS)}",
            "randomUrl": lambda: f"https://{f.word()}.{r.choice(_SAFE_DOMAINS)}/{f.word()}",
            "randomIP": lambda: f"{r.choice(_DOC_NETS)}{r.randint(1, 254)}",
            "randomIPV6": lambda: "2001:db8::" + ":".join(f"{r.randint(0, 65535):x}" for _ in range(3)),
            "randomDatePast": lambda: (now() - _dt.timedelta(days=r.randint(1, 365))).isoformat(),
            "randomDateFuture": lambda: (now() + _dt.timedelta(days=r.randint(1, 365))).isoformat(),
            # --- parametrised helpers ---
            "uuid": self._uuid,
            "now": lambda: now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "today": lambda: _dt.date.today().isoformat(),
            "int": lambda lo="0", hi="1000": r.randint(int(lo), int(hi)),
            "decimal": lambda lo="0", hi="1000", scale="2": round(r.uniform(float(lo), float(hi)), int(scale)),
            "oneOf": lambda *vals: r.choice(vals),
            "pattern": self._pattern,
            "string": self._string,
            "date": self._date,
            "email": lambda domain=None: self._email(domain),
            # --- run-unique values (not reproducible by seed; for uniqueness constraints) ---
            "unique": lambda prefix="": self.unique(prefix),
            "uniqueEmail": lambda domain=None: self._email(domain, unique=True),
            "uniqueUserName": lambda: f"{re.sub(r'[^a-z0-9]', '', self.fake.user_name().lower())}_{self.unique()}",
            "runId": lambda: self.run_id,
            "phone": self._phone,
            # --- negative-testing values ---
            "edgeString": lambda: r.choice(EDGE_STRINGS),
            "sqlInjection": lambda: r.choice(["' OR '1'='1", "Robert'); DROP TABLE users;--", "1; SELECT pg_sleep(5)--"]),
            "xss": lambda: r.choice(["<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>", "javascript:alert(1)"]),
            "unicode": lambda: r.choice(["Zoë Ångström", "李小龙", "مرحبا", "😀🚀✅", "Ünïcödé"]),
            "empty": lambda: "",
            "whitespace": lambda: r.choice([" ", "   ", "\t", "\n"]),
        }
