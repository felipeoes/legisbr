"""Lazy scraper registration and site-specific runtime defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from legisbr.core.query import SearchCapabilities

if TYPE_CHECKING:
    from legisbr.core.scraper import BaseScraper

Scope = Literal["federal", "state", "environment"]


class Source(StrEnum):
    """Canonical legislation source codes (30 official Brazilian portals)."""

    # Federal & environmental
    FEDERAL = "federal"
    CONAMA = "conama"
    ICMBIO = "icmbio"
    # Brazilian states (27 UFs in alphabetical order)
    AC = "ac"
    AL = "al"
    AM = "am"
    AP = "ap"
    BA = "ba"
    CE = "ce"
    DF = "df"
    ES = "es"
    GO = "go"
    MA = "ma"
    MG = "mg"
    MS = "ms"
    MT = "mt"
    PA = "pa"
    PB = "pb"
    PE = "pe"
    PI = "pi"
    PR = "pr"
    RJ = "rj"
    RN = "rn"
    RO = "ro"
    RR = "rr"
    RS = "rs"
    SC = "sc"
    SE = "se"
    SP = "sp"
    TO = "to"


SourceInput: TypeAlias = Source | str

_CLS_CACHE: dict[tuple[str, str], type[BaseScraper]] = {}

_CAP_YEAR_ONLY = SearchCapabilities(native_year=True)
_CAP_YEAR_TYPE = SearchCapabilities(native_year=True, native_type=True)
_CAP_SAPL = SearchCapabilities(
    native_year=True, native_type=True, native_number=True, native_text=True
)


@dataclass(frozen=True)
class SourceInfo:
    """Lightweight, import-free metadata describing a registered source."""

    code: Source
    name: str
    scope: Scope
    aliases: tuple[str, ...]
    start_year: int
    uses_pdf: bool
    needs_ocr: bool
    needs_proxy: bool
    insecure_tls: bool
    capabilities: SearchCapabilities = SearchCapabilities()


@dataclass(frozen=True)
class ScraperSpec:
    name: str
    cls_name: str
    cls_path: str
    code: Source
    scope: Scope
    aliases: tuple[str, ...]
    start_year: int
    uses_pdf: bool = False
    rps: float = 10
    workers: int = 16
    needs_proxy: bool = False
    needs_ocr: bool = False
    max_retries: int = 6
    pause_on_403: float = 0
    disable_cookies: bool = False
    insecure_tls: bool = False
    capabilities: SearchCapabilities = _CAP_YEAR_ONLY
    _cls: type[BaseScraper] | None = field(default=None, repr=False, compare=False)

    def load_cls(self) -> type[BaseScraper]:
        """Lazily import and cache the scraper class."""
        if self._cls is not None:
            return self._cls
        key = (self.cls_path, self.cls_name)
        cached = _CLS_CACHE.get(key)
        if cached is not None:
            return cached
        module_path, _, attr = self.cls_path.partition(":")
        attr_name = attr or self.cls_name
        loaded: type[BaseScraper] = getattr(import_module(module_path), attr_name)
        _CLS_CACHE[key] = loaded
        return loaded

    @property
    def cls(self) -> type[BaseScraper]:
        return self.load_cls()

    @property
    def years(self) -> range:
        return range(self.start_year, datetime.now().year + 1)

    @property
    def names(self) -> set[str]:
        return {
            self.cls_name.lower(),
            self.cls_name.removesuffix("Scraper").lower(),
            self.name.lower(),
            self.code.lower(),
            *(alias.lower() for alias in self.aliases),
        }

    def database_path(
        self,
        save_dir: Path,
        *,
        state_dir: Path | None = None,
        specific_dir: Path | None = None,
    ) -> Path:
        root = save_dir
        if self.scope == "state" and state_dir is not None:
            root = state_dir
        elif self.scope == "environment" and specific_dir is not None:
            root = specific_dir
        return root / self.name / "documents.db"

    def info(self) -> SourceInfo:
        return SourceInfo(
            code=self.code,
            name=self.name,
            scope=self.scope,
            aliases=self.aliases,
            start_year=self.start_year,
            uses_pdf=self.uses_pdf,
            needs_ocr=self.needs_ocr,
            needs_proxy=self.needs_proxy,
            insecure_tls=self.insecure_tls,
            capabilities=self.capabilities,
        )


def _state(
    name: str,
    cls_name: str,
    code: SourceInput,
    aliases: tuple[str, ...],
    start_year: int,
    **kwargs: Any,
) -> ScraperSpec:
    source_code = Source(code)
    return ScraperSpec(
        name=name,
        cls_name=cls_name,
        cls_path=f"legisbr.scrapers.states.{source_code}:{cls_name}",
        code=source_code,
        scope="state",
        aliases=aliases,
        start_year=start_year,
        **kwargs,
    )


SCRAPERS: list[ScraperSpec] = [
    ScraperSpec(
        name="LEGISLACAO_FEDERAL",
        cls_name="CamaraDepScraper",
        cls_path="legisbr.scrapers.federal.camara:CamaraDepScraper",
        code=Source.FEDERAL,
        scope="federal",
        aliases=("Camara",),
        start_year=1808,
        uses_pdf=True,
        rps=100,
        capabilities=SearchCapabilities(
            native_year=True,
            native_type=True,
            native_number=True,
            native_text=True,
            native_situation=True,
        ),
    ),
    ScraperSpec(
        name="CONAMA",
        cls_name="ConamaScraper",
        cls_path="legisbr.scrapers.environment.conama:ConamaScraper",
        code=Source.CONAMA,
        scope="environment",
        aliases=(),
        start_year=1984,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=_CAP_YEAR_ONLY,
    ),
    ScraperSpec(
        name="ICMBIO",
        cls_name="ICMBioScraper",
        cls_path="legisbr.scrapers.environment.icmbio:ICMBioScraper",
        code=Source.ICMBIO,
        scope="environment",
        aliases=(),
        start_year=2016,
        uses_pdf=True,
        disable_cookies=True,
        capabilities=SearchCapabilities(native_year=False),
    ),
    _state(
        "ACRE",
        "AcreLegisScraper",
        Source.AC,
        ("Acre", "AC"),
        1963,
        # legis.ac.gov.br omits its intermediate CA certificate on OpenSSL 3.0 & 3.5.
        insecure_tls=True,
        capabilities=SearchCapabilities(native_year=False, fast_catalog=True),
    ),
    _state(
        "ALAGOAS",
        "AlagoasSefazScraper",
        Source.AL,
        ("Alagoas", "AL"),
        1942,
        uses_pdf=True,
        needs_ocr=True,
        rps=5,
        capabilities=_CAP_YEAR_ONLY,
    ),
    _state(
        "AMAZONAS",
        "LegislaAMScraper",
        Source.AM,
        ("Amazonas", "AM"),
        1956,
        rps=50,
        capabilities=_CAP_YEAR_TYPE,
    ),
    _state(
        "AMAPA",
        "AmapaAlapScraper",
        Source.AP,
        ("Amapa", "AP"),
        1991,
        capabilities=_CAP_YEAR_TYPE,
    ),
    _state(
        "BAHIA",
        "BahiaLegislaScraper",
        Source.BA,
        ("Bahia", "BA"),
        1891,
        max_retries=7,
        rps=1,
        # www.legislabahia.ba.gov.br offers TLS 1.2 static RSA only, rejected by
        # OpenSSL 3.5+ default security level without DEFAULT@SECLEVEL=1.
        insecure_tls=True,
        capabilities=_CAP_YEAR_ONLY,
    ),
    _state(
        "CEARA",
        "CearaAleceScraper",
        Source.CE,
        ("Ceara", "CE"),
        1968,
        needs_ocr=True,
        capabilities=SearchCapabilities(
            native_year=False, fast_catalog=False, heavy_setup=True
        ),
    ),
    _state(
        "DISTRITO_FEDERAL",
        "DFSinjScraper",
        Source.DF,
        ("DF", "DistritoFederal"),
        1922,
        uses_pdf=True,
        needs_ocr=True,
        rps=100,
        workers=100,
        capabilities=_CAP_YEAR_ONLY,
    ),
    _state(
        "ESPIRITO_SANTO",
        "ESAlesScraper",
        Source.ES,
        ("ES", "EspiritoSanto"),
        1958,
        uses_pdf=True,
        needs_ocr=True,
        rps=50,
        capabilities=_CAP_YEAR_ONLY,
    ),
    _state(
        "GOIAS",
        "LegislaGoias",
        Source.GO,
        ("Goias", "GO"),
        1887,
        uses_pdf=True,
        needs_ocr=True,
        rps=100,
        capabilities=_CAP_YEAR_ONLY,
    ),
    _state(
        "MARANHAO",
        "MaranhaoAlemaScraper",
        Source.MA,
        ("Maranhao", "MA"),
        1906,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=_CAP_YEAR_TYPE,
    ),
    _state(
        "MATO_GROSSO_DO_SUL",
        "MSAlemsScraper",
        Source.MS,
        ("MS", "MatoGrossoDoSul"),
        1979,
        capabilities=_CAP_YEAR_TYPE,
    ),
    _state(
        "MATO_GROSSO",
        "MTAlmtScraper",
        Source.MT,
        ("MT", "MatoGrosso"),
        2017,
        needs_ocr=True,
        rps=5,
        capabilities=_CAP_YEAR_ONLY,
    ),
    _state(
        "MINAS_GERAIS",
        "MGAlmgScraper",
        Source.MG,
        ("MG", "MinasGerais"),
        1831,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=SearchCapabilities(
            native_year=True, native_type=True, native_number=True, native_text=True
        ),
    ),
    _state(
        "PARA",
        "ParaAlepaScraper",
        Source.PA,
        ("Para", "PA"),
        1990,
        uses_pdf=True,
        needs_ocr=True,
        rps=5,
        capabilities=_CAP_YEAR_TYPE,
    ),
    _state(
        "PARAIBA",
        "ParaibaAlpbScraper",
        Source.PB,
        ("Paraiba", "PB"),
        1924,
        uses_pdf=True,
        needs_ocr=True,
        workers=2,
        rps=1,
        capabilities=_CAP_SAPL,
    ),
    _state(
        "PARANA",
        "ParanaCVScraper",
        Source.PR,
        ("Parana", "PR"),
        2025,
        # The site allows ~20 requests per rolling minute per IP; beyond that it answers
        # 403 and blocks the client for a fixed 21-30 min. 0.3 rps (18/min) stays under
        # it (180 requests tested clean), and a block pauses every direct request
        # through it. With PROXY_FILE_PATH set, each proxy gets its own 0.3 rps and a
        # blocked or failing one rests, so load on the site is proxies x 0.3 rps.
        # A direct 403 while every proxy rests holds direct workers for the pause window.
        rps=0.3,
        pause_on_403=35 * 60,
        # In one session, a year listing after another type's listing can come back
        # empty; without cookies every request starts a fresh session.
        disable_cookies=True,
        # 20 free proxies at 0.3 rps each need ~64 in flight (pages take ~10 s through
        # them; 32 workers reached half the rate). Without proxies they just queue.
        workers=64,
        needs_proxy=True,
        # A retry after a proxy failure goes through the next proxy.
        max_retries=4,
        capabilities=SearchCapabilities(
            native_year=True,
            native_type=True,
            native_number=True,
            direct_id=True,
        ),
    ),
    _state(
        "PERNAMBUCO",
        "PernambucoAlepeScraper",
        Source.PE,
        ("Pernambuco", "PE"),
        1835,
        capabilities=_CAP_YEAR_ONLY,
    ),
    _state(
        "PIAUI",
        "PiauiAlepiScraper",
        Source.PI,
        ("Piaui", "PI"),
        1922,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=_CAP_SAPL,
    ),
    # RJ documents use a 300 s timeout; 3 attempts caps one document at 15 min.
    _state(
        "RIO_DE_JANEIRO",
        "RJAlerjScraper",
        Source.RJ,
        ("RJ", "RioDeJaneiro"),
        1891,
        max_retries=3,
        capabilities=SearchCapabilities(native_year=False, native_type=True),
    ),
    _state(
        "RIO_GRANDE_DO_NORTE",
        "RNAlrnScraper",
        Source.RN,
        ("RN", "RioGrandeDoNorte"),
        1971,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=_CAP_YEAR_ONLY,
    ),
    _state(
        "RIO_GRANDE_DO_SUL",
        "RSAlrsScraper",
        Source.RS,
        ("RS", "RioGrandeDoSul"),
        1830,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=_CAP_YEAR_ONLY,
    ),
    _state(
        "RONDONIA",
        "RondoniaCotelScraper",
        Source.RO,
        ("Rondonia", "RO"),
        1981,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=_CAP_YEAR_TYPE,
    ),
    _state(
        "RORAIMA",
        "RoraimaAlerScraper",
        Source.RR,
        ("Roraima", "RR"),
        1991,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=_CAP_SAPL,
    ),
    _state(
        "SANTA_CATARINA",
        "SantaCatarinaScraper",
        Source.SC,
        ("SC",),
        1946,
        capabilities=_CAP_YEAR_TYPE,
    ),
    _state(
        "SAO_PAULO",
        "SaoPauloAlespScraper",
        Source.SP,
        ("SaoPaulo", "SP"),
        1835,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=SearchCapabilities(
            native_year=True,
            native_type=True,
            native_number=True,
            native_situation=True,
        ),
    ),
    _state(
        "SERGIPE",
        "SergipeLegsonScraper",
        Source.SE,
        ("Sergipe", "SE"),
        1940,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=_CAP_YEAR_TYPE,
    ),
    _state(
        "TOCANTINS",
        "TocantinsScraper",
        Source.TO,
        ("TO",),
        1989,
        uses_pdf=True,
        needs_ocr=True,
        capabilities=_CAP_YEAR_TYPE,
    ),
]


def sources() -> list[SourceInfo]:
    """Return metadata for all registered legislation sources without importing scrapers."""
    return [spec.info() for spec in SCRAPERS]


def get_spec(name: SourceInput) -> ScraperSpec:
    """Resolve a single source by ``Source`` enum, code, name, class name, or alias."""
    key = name.strip().lower()
    for spec in SCRAPERS:
        if key in spec.names:
            return spec
    raise ValueError(f"Unknown source: {name}")
