"""Lazy scraper registration and site-specific runtime defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from legisbr.core.scraper import BaseScraper

Scope = Literal["federal", "state", "environment"]

_CLS_CACHE: dict[tuple[str, str], type[BaseScraper]] = {}


@dataclass(frozen=True)
class SourceInfo:
    """Lightweight, import-free metadata describing a registered source."""

    code: str
    name: str
    scope: Scope
    aliases: tuple[str, ...]
    start_year: int
    uses_pdf: bool
    needs_ocr: bool
    needs_proxy: bool
    insecure_tls: bool


@dataclass(frozen=True)
class ScraperSpec:
    name: str
    cls_name: str
    cls_path: str
    code: str
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
        )


def _state(
    name: str,
    cls_name: str,
    code: str,
    aliases: tuple[str, ...],
    start_year: int,
    **kwargs: Any,
) -> ScraperSpec:
    return ScraperSpec(
        name=name,
        cls_name=cls_name,
        cls_path=f"legisbr.scrapers.states.{code}:{cls_name}",
        code=code,
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
        code="camara",
        scope="federal",
        aliases=(),
        start_year=1808,
        uses_pdf=True,
        rps=100,
    ),
    ScraperSpec(
        name="CONAMA",
        cls_name="ConamaScraper",
        cls_path="legisbr.scrapers.environment.conama:ConamaScraper",
        code="conama",
        scope="environment",
        aliases=(),
        start_year=1984,
        uses_pdf=True,
        needs_ocr=True,
    ),
    ScraperSpec(
        name="ICMBIO",
        cls_name="ICMBioScraper",
        cls_path="legisbr.scrapers.environment.icmbio:ICMBioScraper",
        code="icmbio",
        scope="environment",
        aliases=(),
        start_year=2016,
        uses_pdf=True,
        disable_cookies=True,
    ),
    _state("ACRE", "AcreLegisScraper", "ac", ("Acre", "AC"), 1963),
    _state(
        "ALAGOAS",
        "AlagoasSefazScraper",
        "al",
        ("Alagoas", "AL"),
        1942,
        uses_pdf=True,
        needs_ocr=True,
        rps=5,
    ),
    _state("AMAZONAS", "LegislaAMScraper", "am", ("Amazonas", "AM"), 1956, rps=50),
    _state("AMAPA", "AmapaAlapScraper", "ap", ("Amapa", "AP"), 1991),
    _state(
        "BAHIA",
        "BahiaLegislaScraper",
        "ba",
        ("Bahia", "BA"),
        1891,
        max_retries=7,
        rps=1,
        insecure_tls=True,
    ),
    _state("CEARA", "CearaAleceScraper", "ce", ("Ceara", "CE"), 1968, needs_ocr=True),
    _state(
        "DISTRITO_FEDERAL",
        "DFSinjScraper",
        "df",
        ("DF", "DistritoFederal"),
        1922,
        uses_pdf=True,
        needs_ocr=True,
        rps=100,
        workers=100,
    ),
    _state(
        "ESPIRITO_SANTO",
        "ESAlesScraper",
        "es",
        ("ES", "EspiritoSanto"),
        1958,
        uses_pdf=True,
        needs_ocr=True,
        rps=50,
    ),
    _state(
        "GOIAS",
        "LegislaGoias",
        "go",
        ("Goias", "GO"),
        1887,
        uses_pdf=True,
        needs_ocr=True,
        rps=100,
    ),
    _state(
        "MARANHAO",
        "MaranhaoAlemaScraper",
        "ma",
        ("Maranhao", "MA"),
        1906,
        uses_pdf=True,
        needs_ocr=True,
    ),
    _state(
        "MATO_GROSSO_DO_SUL",
        "MSAlemsScraper",
        "ms",
        ("MS", "MatoGrossoDoSul"),
        1979,
    ),
    _state(
        "MATO_GROSSO",
        "MTAlmtScraper",
        "mt",
        ("MT", "MatoGrosso"),
        2017,
        needs_ocr=True,
        rps=5,
    ),
    _state(
        "MINAS_GERAIS",
        "MGAlmgScraper",
        "mg",
        ("MG", "MinasGerais"),
        1831,
        uses_pdf=True,
        needs_ocr=True,
    ),
    _state(
        "PARA",
        "ParaAlepaScraper",
        "pa",
        ("Para", "PA"),
        1990,
        uses_pdf=True,
        needs_ocr=True,
        rps=5,
    ),
    _state(
        "PARAIBA",
        "ParaibaAlpbScraper",
        "pb",
        ("Paraiba", "PB"),
        1924,
        uses_pdf=True,
        needs_ocr=True,
        workers=2,
        rps=1,
    ),
    _state(
        "PARANA",
        "ParanaCVScraper",
        "pr",
        ("Parana", "PR"),
        2025,
        # The site allows ~20 requests per rolling minute per IP; beyond that it answers
        # 403 and blocks the client for a fixed 21-30 min. 0.3 rps (18/min) stays under
        # it (180 requests tested clean), and a block pauses every direct request
        # through it. With PROXY_FILE_PATH set, each proxy gets its own 0.3 rps and a
        # blocked or failing one rests, so load on the site is proxies x 0.3 rps.
        # shortcut: a direct 403 while every proxy rests holds the workers that fell
        # back to direct for the whole pause, even after proxies return; seen 0 times.
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
    ),
    _state(
        "PERNAMBUCO",
        "PernambucoAlepeScraper",
        "pe",
        ("Pernambuco", "PE"),
        1835,
    ),
    _state(
        "PIAUI",
        "PiauiAlepiScraper",
        "pi",
        ("Piaui", "PI"),
        1922,
        uses_pdf=True,
        needs_ocr=True,
    ),
    # RJ documents use a 300 s timeout; 3 attempts caps one document at 15 min.
    _state(
        "RIO_DE_JANEIRO",
        "RJAlerjScraper",
        "rj",
        ("RJ", "RioDeJaneiro"),
        1891,
        max_retries=3,
    ),
    _state(
        "RIO_GRANDE_DO_NORTE",
        "RNAlrnScraper",
        "rn",
        ("RN", "RioGrandeDoNorte"),
        1971,
        uses_pdf=True,
        needs_ocr=True,
    ),
    _state(
        "RIO_GRANDE_DO_SUL",
        "RSAlrsScraper",
        "rs",
        ("RS", "RioGrandeDoSul"),
        1830,
        uses_pdf=True,
        needs_ocr=True,
    ),
    _state(
        "RONDONIA",
        "RondoniaCotelScraper",
        "ro",
        ("Rondonia", "RO"),
        1981,
        uses_pdf=True,
        needs_ocr=True,
    ),
    _state(
        "RORAIMA",
        "RoraimaAlerScraper",
        "rr",
        ("Roraima", "RR"),
        1991,
        uses_pdf=True,
        needs_ocr=True,
    ),
    _state("SANTA_CATARINA", "SantaCatarinaScraper", "sc", ("SC",), 1946),
    _state(
        "SAO_PAULO",
        "SaoPauloAlespScraper",
        "sp",
        ("SaoPaulo", "SP"),
        1835,
        uses_pdf=True,
        needs_ocr=True,
    ),
    _state(
        "SERGIPE",
        "SergipeLegsonScraper",
        "se",
        ("Sergipe", "SE"),
        1940,
        uses_pdf=True,
        needs_ocr=True,
    ),
    _state(
        "TOCANTINS",
        "TocantinsScraper",
        "to",
        ("TO",),
        1989,
        uses_pdf=True,
        needs_ocr=True,
    ),
]


def sources() -> list[SourceInfo]:
    """Return metadata for all registered legislation sources without importing scrapers."""
    return [spec.info() for spec in SCRAPERS]


def get_spec(name: str) -> ScraperSpec:
    """Resolve a single source by code, name, class name, or alias."""
    key = name.strip().lower()
    for spec in SCRAPERS:
        if key in spec.names:
            return spec
    raise ValueError(f"Unknown source: {name}")
