from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
import unicodedata
from typing import Any, Literal


class InvalidFootballApiRequest(ValueError):
    """Raised before API-Football transport when a request is not compiled."""


_COMPILER_TOKEN = object()
_SLOT_TOKEN = object()

CandidateSource = Literal[
    "planner",
    "deterministic_parser",
    "slash_arg",
    "validated_context",
    "alias",
    "canonicalizer",
]
CandidateAuthority = Literal[
    "EXPLICIT_CURRENT_MESSAGE",
    "EXPLICIT_REPLY_TARGET",
    "VALIDATED_FOOTBALL_CONTEXT",
    "DERIVED_ALIAS",
    "CANONICAL_EQUIVALENT",
]

_INTENT_WORDS = {
    "ahora",
    "carrera",
    "como",
    "cuantos",
    "cual",
    "dame",
    "darme",
    "dime",
    "donde",
    "equipo",
    "estadistica",
    "estadisticas",
    "goals",
    "goles",
    "historial",
    "injuries",
    "juega",
    "lesiones",
    "podrias",
    "recientes",
    "stats",
    "team",
    "transferencias",
    "ultimo",
    "where",
}
_FORBIDDEN_LITERALS = {"", "false", "true", "none", "null", "undefined", "nan"}
_FORBIDDEN_FRAGMENTS = {
    "actual",
    "ahora",
    "anterior",
    "equipo",
    "juega",
    "league",
    "liga",
    "match",
    "partido",
    "plate",
    "recent",
    "reciente",
    "stats",
    "team",
}


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def normalize_key(value: str | None) -> str:
    text = normalize_text(value)
    return re.sub(r"[^a-z0-9]+", "", text)


def words(value: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize_text(value))


def _clean_text(value: Any, *, label: str, max_words: int = 6, allow_single_fragment: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidFootballApiRequest(f"{label} must be validated text.")
    cleaned = " ".join(value.split())
    key = normalize_key(cleaned)
    if key in _FORBIDDEN_LITERALS:
        raise InvalidFootballApiRequest(f"{label} is empty or non-semantic.")
    value_words = words(cleaned)
    if not value_words:
        raise InvalidFootballApiRequest(f"{label} is empty.")
    if "?" in cleaned or "¿" in cleaned:
        raise InvalidFootballApiRequest(f"{label} looks like a question.")
    if len(value_words) > max_words:
        raise InvalidFootballApiRequest(f"{label} is too broad.")
    if sum(1 for word in value_words if word in _INTENT_WORDS) >= 2:
        raise InvalidFootballApiRequest(f"{label} looks like request text.")
    if not allow_single_fragment and len(value_words) == 1 and value_words[0] in _FORBIDDEN_FRAGMENTS:
        raise InvalidFootballApiRequest(f"{label} is an unsafe fragment.")
    if len(key) < 2:
        raise InvalidFootballApiRequest(f"{label} is too short.")
    return cleaned[:120]


def _clean_slot_label(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise InvalidFootballApiRequest(f"{label} must be structured text.")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise InvalidFootballApiRequest(f"{label} is empty.")
    if len(cleaned) > 120:
        raise InvalidFootballApiRequest(f"{label} is too long.")
    if any(ord(ch) < 32 for ch in cleaned):
        raise InvalidFootballApiRequest(f"{label} contains control characters.")
    if normalize_key(cleaned) in _FORBIDDEN_LITERALS:
        raise InvalidFootballApiRequest(f"{label} is non-semantic.")
    return cleaned


def _validate_confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidFootballApiRequest("slot confidence must be numeric.")
    confidence = float(value)
    if confidence < 0.0 or confidence > 1.0:
        raise InvalidFootballApiRequest("slot confidence is outside supported range.")
    return confidence


def validate_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidFootballApiRequest(f"{label} must be a positive integer.")
    return value


class _OpaqueText:
    __slots__ = ("_value",)

    def __init__(self, value: str, *, _token: object) -> None:
        if _token is not _COMPILER_TOKEN:
            raise InvalidFootballApiRequest("validated values must be created by compiler factories.")
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<validated>)"

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and getattr(other, "_value", None) == self._value

    def __hash__(self) -> int:
        return hash((type(self), self._value))


class ValidatedPlayerName(_OpaqueText):
    pass


class ValidatedPlayerLastname(_OpaqueText):
    pass


class ValidatedTeamName(_OpaqueText):
    pass


class ValidatedLeagueName(_OpaqueText):
    pass


class ValidatedCountryName(_OpaqueText):
    pass


class ValidatedVenueName(_OpaqueText):
    pass


class ValidatedCoachName(_OpaqueText):
    pass


class ValidatedOddsLabel(_OpaqueText):
    pass


class ValidatedDate(_OpaqueText):
    pass


class ValidatedSeason:
    __slots__ = ("_value",)

    def __init__(self, value: int, *, _token: object) -> None:
        if _token is not _COMPILER_TOKEN:
            raise InvalidFootballApiRequest("validated values must be created by compiler factories.")
        if value < 1900 or value > 2100:
            raise InvalidFootballApiRequest("season is outside supported range.")
        self._value = value

    @property
    def value(self) -> int:
        return self._value

    def __int__(self) -> int:
        return self._value

    def __repr__(self) -> str:
        return "ValidatedSeason(<validated>)"


@dataclass(frozen=True)
class PlayerSlot:
    full_name: str
    source: CandidateSource
    confidence: float = 1.0
    literal: str | None = None
    authority: CandidateAuthority = "EXPLICIT_CURRENT_MESSAGE"
    source_component: str | None = None
    evidence: str | None = None
    equivalent_to: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    team_hint: str | None = None
    league_hint: str | None = None
    country_hint: str | None = None
    nationality_hint: str | None = None
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _SLOT_TOKEN:
            raise InvalidFootballApiRequest("player slots must be created by the football interpreter.")
        full_name = _clean_slot_label(self.full_name, label="player slot")
        object.__setattr__(self, "full_name", full_name)
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence))
        object.__setattr__(self, "literal", _clean_slot_label(self.literal, label="player literal") if self.literal else full_name)
        if self.source_component is not None:
            object.__setattr__(self, "source_component", _clean_slot_label(self.source_component, label="player source component"))
        if self.evidence is not None:
            object.__setattr__(self, "evidence", _clean_slot_label(self.evidence, label="player evidence"))
        if self.equivalent_to is not None:
            object.__setattr__(self, "equivalent_to", _clean_slot_label(self.equivalent_to, label="player equivalent"))
        if self.first_name is not None:
            object.__setattr__(self, "first_name", _clean_slot_label(self.first_name, label="player first name"))
        if self.last_name is not None:
            object.__setattr__(self, "last_name", _clean_slot_label(self.last_name, label="player last name"))


@dataclass(frozen=True)
class TeamSlot:
    name: str
    source: CandidateSource
    confidence: float = 1.0
    literal: str | None = None
    authority: CandidateAuthority = "EXPLICIT_CURRENT_MESSAGE"
    source_component: str | None = None
    evidence: str | None = None
    equivalent_to: str | None = None
    league_hint: str | None = None
    country_hint: str | None = None
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _SLOT_TOKEN:
            raise InvalidFootballApiRequest("team slots must be created by the football interpreter.")
        name = _clean_slot_label(self.name, label="team slot")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence))
        object.__setattr__(self, "literal", _clean_slot_label(self.literal, label="team literal") if self.literal else name)
        if self.source_component is not None:
            object.__setattr__(self, "source_component", _clean_slot_label(self.source_component, label="team source component"))
        if self.evidence is not None:
            object.__setattr__(self, "evidence", _clean_slot_label(self.evidence, label="team evidence"))
        if self.equivalent_to is not None:
            object.__setattr__(self, "equivalent_to", _clean_slot_label(self.equivalent_to, label="team equivalent"))


@dataclass(frozen=True)
class LeagueSlot:
    name: str
    source: CandidateSource
    confidence: float = 1.0
    literal: str | None = None
    authority: CandidateAuthority = "EXPLICIT_CURRENT_MESSAGE"
    source_component: str | None = None
    evidence: str | None = None
    equivalent_to: str | None = None
    country_hint: str | None = None
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _SLOT_TOKEN:
            raise InvalidFootballApiRequest("league slots must be created by the football interpreter.")
        name = _clean_slot_label(self.name, label="league slot")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence))
        object.__setattr__(self, "literal", _clean_slot_label(self.literal, label="league literal") if self.literal else name)
        if self.source_component is not None:
            object.__setattr__(self, "source_component", _clean_slot_label(self.source_component, label="league source component"))
        if self.evidence is not None:
            object.__setattr__(self, "evidence", _clean_slot_label(self.evidence, label="league evidence"))
        if self.equivalent_to is not None:
            object.__setattr__(self, "equivalent_to", _clean_slot_label(self.equivalent_to, label="league equivalent"))


@dataclass(frozen=True)
class CountrySlot:
    name: str
    source: CandidateSource
    confidence: float = 1.0
    literal: str | None = None
    authority: CandidateAuthority = "EXPLICIT_CURRENT_MESSAGE"
    source_component: str | None = None
    evidence: str | None = None
    equivalent_to: str | None = None
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _SLOT_TOKEN:
            raise InvalidFootballApiRequest("country slots must be created by the football interpreter.")
        name = _clean_slot_label(self.name, label="country slot")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "confidence", _validate_confidence(self.confidence))
        object.__setattr__(self, "literal", _clean_slot_label(self.literal, label="country literal") if self.literal else name)
        if self.source_component is not None:
            object.__setattr__(self, "source_component", _clean_slot_label(self.source_component, label="country source component"))
        if self.evidence is not None:
            object.__setattr__(self, "evidence", _clean_slot_label(self.evidence, label="country evidence"))
        if self.equivalent_to is not None:
            object.__setattr__(self, "equivalent_to", _clean_slot_label(self.equivalent_to, label="country equivalent"))


@dataclass(frozen=True)
class FootballCapabilityIntent:
    operation_family: str
    data_focus: str | None = None
    temporal_semantics: str | None = None
    requested_subscope: str | None = None
    authority: CandidateAuthority = "EXPLICIT_CURRENT_MESSAGE"
    source_component: str = "football_query_service"
    evidence: str | None = None
    planner_operation: str | None = None
    planner_data_focus: str | None = None
    planner_accepted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_family", _clean_slot_label(self.operation_family, label="capability operation"))
        if self.data_focus is not None:
            object.__setattr__(self, "data_focus", _clean_slot_label(self.data_focus, label="capability data focus"))
        if self.temporal_semantics is not None:
            object.__setattr__(self, "temporal_semantics", _clean_slot_label(self.temporal_semantics, label="capability temporal semantics"))
        if self.requested_subscope is not None:
            object.__setattr__(self, "requested_subscope", _clean_slot_label(self.requested_subscope, label="capability subscope"))
        if self.source_component is not None:
            object.__setattr__(self, "source_component", _clean_slot_label(self.source_component, label="capability source component"))
        if self.evidence is not None:
            object.__setattr__(self, "evidence", _clean_slot_label(self.evidence, label="capability evidence"))
        if self.planner_operation is not None:
            object.__setattr__(self, "planner_operation", _clean_slot_label(self.planner_operation, label="planner operation"))
        if self.planner_data_focus is not None:
            object.__setattr__(self, "planner_data_focus", _clean_slot_label(self.planner_data_focus, label="planner data focus"))


PlayerCandidate = PlayerSlot
TeamCandidate = TeamSlot
LeagueCandidate = LeagueSlot
CountryCandidate = CountrySlot


@dataclass(frozen=True)
class ResolvedPlayer:
    player_id: int
    name: str
    row: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedTeam:
    team_id: int
    name: str
    row: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedLeague:
    league_id: int
    name: str
    season: int | None = None
    row: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedFixture:
    fixture_id: int
    row: dict[str, Any] | None = None


@dataclass(frozen=True)
class LeagueSearchRequest:
    name: ValidatedLeagueName | None = None
    country: ValidatedCountryName | None = None
    search: ValidatedLeagueName | None = None
    current: bool | None = None

    def __post_init__(self) -> None:
        if self.name is None and self.country is None and self.search is None:
            raise InvalidFootballApiRequest("league search requires a validated field.")
        if self.current is not None and not isinstance(self.current, bool):
            raise InvalidFootballApiRequest("current must be boolean.")


@dataclass(frozen=True)
class TeamSearchRequest:
    name: ValidatedTeamName | None = None
    search: ValidatedTeamName | None = None
    league_id: int | None = None
    season: ValidatedSeason | None = None

    def __post_init__(self) -> None:
        if self.name is None and self.search is None:
            raise InvalidFootballApiRequest("team search requires a validated team name.")
        if self.name is not None and not isinstance(self.name, ValidatedTeamName):
            raise InvalidFootballApiRequest("team search requires a validated team name.")
        if self.search is not None and not isinstance(self.search, ValidatedTeamName):
            raise InvalidFootballApiRequest("team search requires a validated team search.")
        _validate_optional_id(self.league_id, "league_id")


@dataclass(frozen=True)
class PlayerProfileRequest:
    lastname: ValidatedPlayerLastname

    def __post_init__(self) -> None:
        if not isinstance(self.lastname, ValidatedPlayerLastname):
            raise InvalidFootballApiRequest("profile search requires a validated player lastname.")


@dataclass(frozen=True)
class PlayerSearchRequest:
    name: ValidatedPlayerName | ValidatedPlayerLastname
    league_id: int | None = None
    season: ValidatedSeason | None = None
    team_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, (ValidatedPlayerName, ValidatedPlayerLastname)):
            raise InvalidFootballApiRequest("player search requires a validated player name.")
        _validate_optional_id(self.league_id, "league_id")
        _validate_optional_id(self.team_id, "team_id")


@dataclass(frozen=True)
class PlayerStatsRequest:
    player_id: int
    season: ValidatedSeason
    league_id: int | None = None
    team_id: int | None = None

    def __post_init__(self) -> None:
        validate_positive_int(self.player_id, "player_id")
        if not isinstance(self.season, ValidatedSeason):
            raise InvalidFootballApiRequest("player stats requires a validated season.")
        _validate_optional_id(self.league_id, "league_id")
        _validate_optional_id(self.team_id, "team_id")


@dataclass(frozen=True)
class PlayerSeasonsRequest:
    player_id: int

    def __post_init__(self) -> None:
        validate_positive_int(self.player_id, "player_id")


@dataclass(frozen=True)
class PlayerSquadsRequest:
    player_id: int | None = None
    team_id: int | None = None

    def __post_init__(self) -> None:
        _require_any_id(player_id=self.player_id, team_id=self.team_id)


@dataclass(frozen=True)
class InjuryRequest:
    league_id: int | None = None
    season: ValidatedSeason | None = None
    team_id: int | None = None
    player_id: int | None = None
    fixture_id: int | None = None

    def __post_init__(self) -> None:
        _require_any_id(league_id=self.league_id, team_id=self.team_id, player_id=self.player_id, fixture_id=self.fixture_id)


@dataclass(frozen=True)
class TransferRequest:
    team_id: int | None = None
    player_id: int | None = None

    def __post_init__(self) -> None:
        _require_any_id(team_id=self.team_id, player_id=self.player_id)


@dataclass(frozen=True)
class FixtureIdsRequest:
    fixture_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.fixture_ids, tuple) or not self.fixture_ids:
            raise InvalidFootballApiRequest("fixture ids request requires fixture IDs.")
        if len(self.fixture_ids) > 20:
            raise InvalidFootballApiRequest("fixture ids request supports at most 20 IDs.")
        seen: set[int] = set()
        for fixture_id in self.fixture_ids:
            validate_positive_int(fixture_id, "fixture_id")
            if fixture_id in seen:
                raise InvalidFootballApiRequest("fixture ids request contains duplicates.")
            seen.add(fixture_id)


@dataclass(frozen=True)
class FixtureStatisticsRequest:
    fixture_id: int
    team_id: int | None = None
    stat_type: str | None = None
    half: bool = False

    def __post_init__(self) -> None:
        validate_positive_int(self.fixture_id, "fixture_id")
        _validate_optional_id(self.team_id, "team_id")
        if self.stat_type is not None:
            _clean_text(self.stat_type, label="fixture statistic type", max_words=5, allow_single_fragment=True)
        if not isinstance(self.half, bool):
            raise InvalidFootballApiRequest("half must be boolean.")


@dataclass(frozen=True)
class FixturePlayersRequest:
    fixture_id: int
    team_id: int | None = None

    def __post_init__(self) -> None:
        validate_positive_int(self.fixture_id, "fixture_id")
        _validate_optional_id(self.team_id, "team_id")


@dataclass(frozen=True)
class FixtureRoundsRequest:
    league_id: int
    season: ValidatedSeason
    current: bool | None = None
    include_dates: bool = False

    def __post_init__(self) -> None:
        validate_positive_int(self.league_id, "league_id")
        if not isinstance(self.season, ValidatedSeason):
            raise InvalidFootballApiRequest("rounds request requires a validated season.")
        if self.current is not None and not isinstance(self.current, bool):
            raise InvalidFootballApiRequest("current must be boolean.")
        if not isinstance(self.include_dates, bool):
            raise InvalidFootballApiRequest("include_dates must be boolean.")


@dataclass(frozen=True)
class TeamStatisticsRequest:
    league_id: int
    season: ValidatedSeason
    team_id: int
    date: ValidatedDate | None = None

    def __post_init__(self) -> None:
        validate_positive_int(self.league_id, "league_id")
        validate_positive_int(self.team_id, "team_id")
        if not isinstance(self.season, ValidatedSeason):
            raise InvalidFootballApiRequest("team statistics request requires a validated season.")
        if self.date is not None and not isinstance(self.date, ValidatedDate):
            raise InvalidFootballApiRequest("team statistics date must be validated.")


@dataclass(frozen=True)
class TeamSeasonsRequest:
    team_id: int

    def __post_init__(self) -> None:
        validate_positive_int(self.team_id, "team_id")


@dataclass(frozen=True)
class VenueSearchRequest:
    name: ValidatedVenueName | None = None
    search: ValidatedVenueName | None = None
    city: ValidatedVenueName | None = None
    country: ValidatedCountryName | None = None

    def __post_init__(self) -> None:
        if self.name is None and self.search is None and self.city is None and self.country is None:
            raise InvalidFootballApiRequest("venue search requires a validated field.")
        for value, label, cls in (
            (self.name, "venue name", ValidatedVenueName),
            (self.search, "venue search", ValidatedVenueName),
            (self.city, "venue city", ValidatedVenueName),
            (self.country, "venue country", ValidatedCountryName),
        ):
            if value is not None and not isinstance(value, cls):
                raise InvalidFootballApiRequest(f"{label} must be validated.")


@dataclass(frozen=True)
class PlayerTeamsRequest:
    player_id: int

    def __post_init__(self) -> None:
        validate_positive_int(self.player_id, "player_id")


@dataclass(frozen=True)
class CoachSearchRequest:
    coach_id: int | None = None
    team_id: int | None = None
    search: ValidatedCoachName | None = None

    def __post_init__(self) -> None:
        _require_any_id(coach_id=self.coach_id, team_id=self.team_id) if self.search is None else None
        _validate_optional_id(self.coach_id, "coach_id")
        _validate_optional_id(self.team_id, "team_id")
        if self.search is not None and not isinstance(self.search, ValidatedCoachName):
            raise InvalidFootballApiRequest("coach search must be validated.")


@dataclass(frozen=True)
class MultiEntityRequest:
    player_id: int | None = None
    coach_id: int | None = None
    player_ids: tuple[int, ...] = ()
    coach_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.player_id is None and self.coach_id is None and not self.player_ids and not self.coach_ids:
            raise InvalidFootballApiRequest("request requires at least one validated entity ID.")
        _validate_optional_id(self.player_id, "player_id")
        _validate_optional_id(self.coach_id, "coach_id")
        _validate_id_batch(self.player_ids, "player_ids", max_value=20)
        _validate_id_batch(self.coach_ids, "coach_ids", max_value=20)


@dataclass(frozen=True)
class PredictionRequest:
    fixture_id: int

    def __post_init__(self) -> None:
        validate_positive_int(self.fixture_id, "fixture_id")


@dataclass(frozen=True)
class OddsRequest:
    fixture_id: int | None = None
    league_id: int | None = None
    season: ValidatedSeason | None = None
    date: ValidatedDate | None = None
    bookmaker_id: int | None = None
    bet_id: int | None = None
    live: bool = False

    def __post_init__(self) -> None:
        _validate_optional_id(self.fixture_id, "fixture_id")
        _validate_optional_id(self.league_id, "league_id")
        _validate_optional_id(self.bookmaker_id, "bookmaker_id")
        _validate_optional_id(self.bet_id, "bet_id")
        if self.season is not None and not isinstance(self.season, ValidatedSeason):
            raise InvalidFootballApiRequest("odds season must be validated.")
        if self.date is not None and not isinstance(self.date, ValidatedDate):
            raise InvalidFootballApiRequest("odds date must be validated.")
        if not isinstance(self.live, bool):
            raise InvalidFootballApiRequest("live must be boolean.")
        if self.live:
            if self.fixture_id is None and self.league_id is None and self.bet_id is None:
                raise InvalidFootballApiRequest("live odds require fixture, league, or bet scope.")
            if self.season is not None or self.date is not None or self.bookmaker_id is not None:
                raise InvalidFootballApiRequest("live odds do not support season, date, or bookmaker filters.")
        elif self.fixture_id is None and self.league_id is None and self.date is None:
            raise InvalidFootballApiRequest("odds require fixture, league, or date scope.")
        if self.league_id is not None and not self.live and self.season is None:
            raise InvalidFootballApiRequest("league-scoped odds require a validated season.")


@dataclass(frozen=True)
class OddsReferenceRequest:
    item_id: int | None = None
    search: ValidatedOddsLabel | None = None

    def __post_init__(self) -> None:
        _validate_optional_id(self.item_id, "item_id")
        if self.search is not None and not isinstance(self.search, ValidatedOddsLabel):
            raise InvalidFootballApiRequest("odds reference search must be validated.")


def _make_player_slot(
    label: Any,
    *,
    source: CandidateSource,
    confidence: float = 1.0,
    literal: str | None = None,
    authority: CandidateAuthority | None = None,
    source_component: str | None = None,
    evidence: str | None = None,
    equivalent_to: str | None = None,
    team_hint: str | None = None,
    league_hint: str | None = None,
    country_hint: str | None = None,
    nationality_hint: str | None = None,
) -> PlayerSlot:
    cleaned = _clean_slot_label(label, label="player slot")
    parts = cleaned.split()
    first = parts[0] if len(parts) >= 2 else None
    last = parts[-1] if parts else None
    if last is None:
        raise InvalidFootballApiRequest("player slot needs a name.")
    return PlayerSlot(
        full_name=cleaned,
        source=source,
        confidence=confidence,
        literal=literal,
        authority=authority or _authority_for_source(source),
        source_component=source_component,
        evidence=evidence,
        equivalent_to=equivalent_to,
        first_name=first,
        last_name=last,
        team_hint=team_hint,
        league_hint=league_hint,
        country_hint=country_hint,
        nationality_hint=nationality_hint,
        _token=_SLOT_TOKEN,
    )


def _make_team_slot(
    label: Any,
    *,
    source: CandidateSource,
    confidence: float = 1.0,
    literal: str | None = None,
    authority: CandidateAuthority | None = None,
    source_component: str | None = None,
    evidence: str | None = None,
    equivalent_to: str | None = None,
    league_hint: str | None = None,
    country_hint: str | None = None,
) -> TeamSlot:
    return TeamSlot(
        name=_clean_slot_label(label, label="team slot"),
        source=source,
        confidence=confidence,
        literal=literal,
        authority=authority or _authority_for_source(source),
        source_component=source_component,
        evidence=evidence,
        equivalent_to=equivalent_to,
        league_hint=league_hint,
        country_hint=country_hint,
        _token=_SLOT_TOKEN,
    )


def _make_league_slot(
    label: Any,
    *,
    source: CandidateSource,
    confidence: float = 1.0,
    literal: str | None = None,
    authority: CandidateAuthority | None = None,
    source_component: str | None = None,
    evidence: str | None = None,
    equivalent_to: str | None = None,
    country_hint: str | None = None,
) -> LeagueSlot:
    return LeagueSlot(
        name=_clean_slot_label(label, label="league slot"),
        source=source,
        confidence=confidence,
        literal=literal,
        authority=authority or _authority_for_source(source),
        source_component=source_component,
        evidence=evidence,
        equivalent_to=equivalent_to,
        country_hint=country_hint,
        _token=_SLOT_TOKEN,
    )


def _make_country_slot(
    label: Any,
    *,
    source: CandidateSource,
    confidence: float = 1.0,
    literal: str | None = None,
    authority: CandidateAuthority | None = None,
    source_component: str | None = None,
    evidence: str | None = None,
    equivalent_to: str | None = None,
) -> CountrySlot:
    return CountrySlot(
        name=_clean_slot_label(label, label="country slot"),
        source=source,
        confidence=confidence,
        literal=literal,
        authority=authority or _authority_for_source(source),
        source_component=source_component,
        evidence=evidence,
        equivalent_to=equivalent_to,
        _token=_SLOT_TOKEN,
    )


def _authority_for_source(source: CandidateSource) -> CandidateAuthority:
    if source == "validated_context":
        return "VALIDATED_FOOTBALL_CONTEXT"
    if source == "alias":
        return "DERIVED_ALIAS"
    if source == "canonicalizer":
        return "CANONICAL_EQUIVALENT"
    return "EXPLICIT_CURRENT_MESSAGE"


def make_player_candidate(*_args: Any, **_kwargs: Any) -> PlayerSlot:
    raise InvalidFootballApiRequest("raw player candidate factories were removed; use FootballQuerySpec slots.")


def make_team_candidate(*_args: Any, **_kwargs: Any) -> TeamSlot:
    raise InvalidFootballApiRequest("raw team candidate factories were removed; use FootballQuerySpec slots.")


def make_league_candidate(*_args: Any, **_kwargs: Any) -> LeagueSlot:
    raise InvalidFootballApiRequest("raw league candidate factories were removed; use FootballQuerySpec slots.")


def make_country_candidate(*_args: Any, **_kwargs: Any) -> CountrySlot:
    raise InvalidFootballApiRequest("raw country candidate factories were removed; use FootballQuerySpec slots.")


def make_validated_season(value: Any) -> ValidatedSeason:
    return ValidatedSeason(validate_positive_int(value, "season"), _token=_COMPILER_TOKEN)


def make_validated_date(value: Any) -> ValidatedDate:
    if isinstance(value, date) and not isinstance(value, datetime):
        text = value.isoformat()
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise InvalidFootballApiRequest("date must be YYYY-MM-DD.")
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", text):
        raise InvalidFootballApiRequest("date must be YYYY-MM-DD.")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidFootballApiRequest("date is invalid.") from exc
    return ValidatedDate(text, _token=_COMPILER_TOKEN)


def build_player_profile_request(candidate: PlayerSlot) -> PlayerProfileRequest:
    if not isinstance(candidate, PlayerSlot) or not candidate.last_name:
        raise InvalidFootballApiRequest("player profile search requires a compiled player candidate.")
    last = _clean_text(candidate.last_name, label="player lastname", max_words=1, allow_single_fragment=False)
    return PlayerProfileRequest(ValidatedPlayerLastname(last, _token=_COMPILER_TOKEN))


def build_player_search_request(
    candidate: PlayerSlot,
    *,
    league_id: int | None = None,
    season: int | None = None,
    team_id: int | None = None,
) -> PlayerSearchRequest:
    if not isinstance(candidate, PlayerSlot):
        raise InvalidFootballApiRequest("player search requires a compiled player candidate.")
    season_value = make_validated_season(season) if season is not None else None
    name = ValidatedPlayerName(_clean_text(candidate.full_name, label="player name", max_words=5, allow_single_fragment=True), _token=_COMPILER_TOKEN)
    return PlayerSearchRequest(name=name, league_id=league_id, season=season_value, team_id=team_id)


def build_team_search_request(candidate: TeamSlot, *, league_id: int | None = None, season: int | None = None, search: bool = False) -> TeamSearchRequest:
    if not isinstance(candidate, TeamSlot):
        raise InvalidFootballApiRequest("team search requires a compiled team candidate.")
    season_value = make_validated_season(season) if season is not None else None
    value = ValidatedTeamName(candidate.name, _token=_COMPILER_TOKEN)
    if search:
        return TeamSearchRequest(search=value, league_id=league_id, season=season_value)
    return TeamSearchRequest(name=value, league_id=league_id, season=season_value)


def build_league_search_request(
    candidate: LeagueSlot | None = None,
    *,
    country: CountrySlot | None = None,
    search: LeagueSlot | None = None,
    current: bool | None = None,
) -> LeagueSearchRequest:
    name_value = ValidatedLeagueName(candidate.name, _token=_COMPILER_TOKEN) if isinstance(candidate, LeagueSlot) else None
    search_value = ValidatedLeagueName(search.name, _token=_COMPILER_TOKEN) if isinstance(search, LeagueSlot) else None
    country_value = ValidatedCountryName(country.name, _token=_COMPILER_TOKEN) if isinstance(country, CountrySlot) else None
    return LeagueSearchRequest(name=name_value, country=country_value, search=search_value, current=current)


def build_player_stats_request(
    *,
    player_id: int,
    season: int,
    league_id: int | None = None,
    team_id: int | None = None,
) -> PlayerStatsRequest:
    return PlayerStatsRequest(player_id=player_id, season=make_validated_season(season), league_id=league_id, team_id=team_id)


def build_player_seasons_request(*, player_id: int) -> PlayerSeasonsRequest:
    return PlayerSeasonsRequest(player_id=player_id)


def build_player_squads_request(*, player_id: int | None = None, team_id: int | None = None) -> PlayerSquadsRequest:
    return PlayerSquadsRequest(player_id=player_id, team_id=team_id)


def build_injury_request(
    *,
    league_id: int | None = None,
    season: int | None = None,
    team_id: int | None = None,
    player_id: int | None = None,
    fixture_id: int | None = None,
) -> InjuryRequest:
    return InjuryRequest(
        league_id=league_id,
        season=make_validated_season(season) if season is not None else None,
        team_id=team_id,
        player_id=player_id,
        fixture_id=fixture_id,
    )


def build_transfer_request(*, team_id: int | None = None, player_id: int | None = None) -> TransferRequest:
    return TransferRequest(team_id=team_id, player_id=player_id)


def build_fixture_ids_request(fixture_ids: list[int] | tuple[int, ...]) -> FixtureIdsRequest:
    return FixtureIdsRequest(tuple(fixture_ids))


def build_fixture_statistics_request(
    *,
    fixture_id: int,
    team_id: int | None = None,
    stat_type: str | None = None,
    half: bool = False,
) -> FixtureStatisticsRequest:
    return FixtureStatisticsRequest(fixture_id=fixture_id, team_id=team_id, stat_type=stat_type, half=half)


def build_fixture_players_request(*, fixture_id: int, team_id: int | None = None) -> FixturePlayersRequest:
    return FixturePlayersRequest(fixture_id=fixture_id, team_id=team_id)


def build_fixture_rounds_request(*, league_id: int, season: int, current: bool | None = None, include_dates: bool = False) -> FixtureRoundsRequest:
    return FixtureRoundsRequest(league_id=league_id, season=make_validated_season(season), current=current, include_dates=include_dates)


def build_team_statistics_request(*, league_id: int, season: int, team_id: int, date_iso: str | None = None) -> TeamStatisticsRequest:
    return TeamStatisticsRequest(
        league_id=league_id,
        season=make_validated_season(season),
        team_id=team_id,
        date=make_validated_date(date_iso) if date_iso is not None else None,
    )


def build_team_seasons_request(*, team_id: int) -> TeamSeasonsRequest:
    return TeamSeasonsRequest(team_id=team_id)


def build_venue_search_request(
    *,
    name: str | None = None,
    search: str | None = None,
    city: str | None = None,
    country: CountrySlot | None = None,
) -> VenueSearchRequest:
    return VenueSearchRequest(
        name=ValidatedVenueName(_clean_text(name, label="venue name", max_words=8, allow_single_fragment=True), _token=_COMPILER_TOKEN) if name else None,
        search=ValidatedVenueName(_clean_text(search, label="venue search", max_words=8, allow_single_fragment=True), _token=_COMPILER_TOKEN) if search else None,
        city=ValidatedVenueName(_clean_text(city, label="venue city", max_words=6, allow_single_fragment=True), _token=_COMPILER_TOKEN) if city else None,
        country=ValidatedCountryName(country.name, _token=_COMPILER_TOKEN) if isinstance(country, CountrySlot) else None,
    )


def build_player_teams_request(*, player_id: int) -> PlayerTeamsRequest:
    return PlayerTeamsRequest(player_id=player_id)


def build_coach_search_request(*, coach_id: int | None = None, team_id: int | None = None, search: str | None = None) -> CoachSearchRequest:
    return CoachSearchRequest(
        coach_id=coach_id,
        team_id=team_id,
        search=ValidatedCoachName(_clean_text(search, label="coach search", max_words=5, allow_single_fragment=True), _token=_COMPILER_TOKEN) if search else None,
    )


def build_trophy_request(
    *,
    player_id: int | None = None,
    coach_id: int | None = None,
    player_ids: list[int] | tuple[int, ...] = (),
    coach_ids: list[int] | tuple[int, ...] = (),
) -> MultiEntityRequest:
    return MultiEntityRequest(player_id=player_id, coach_id=coach_id, player_ids=tuple(player_ids), coach_ids=tuple(coach_ids))


def build_sidelined_request(
    *,
    player_id: int | None = None,
    coach_id: int | None = None,
    player_ids: list[int] | tuple[int, ...] = (),
    coach_ids: list[int] | tuple[int, ...] = (),
) -> MultiEntityRequest:
    return MultiEntityRequest(player_id=player_id, coach_id=coach_id, player_ids=tuple(player_ids), coach_ids=tuple(coach_ids))


def build_prediction_request(*, fixture_id: int) -> PredictionRequest:
    return PredictionRequest(fixture_id=fixture_id)


def build_odds_request(
    *,
    fixture_id: int | None = None,
    league_id: int | None = None,
    season: int | None = None,
    date_iso: str | None = None,
    bookmaker_id: int | None = None,
    bet_id: int | None = None,
    live: bool = False,
) -> OddsRequest:
    return OddsRequest(
        fixture_id=fixture_id,
        league_id=league_id,
        season=make_validated_season(season) if season is not None else None,
        date=make_validated_date(date_iso) if date_iso is not None else None,
        bookmaker_id=bookmaker_id,
        bet_id=bet_id,
        live=live,
    )


def build_odds_reference_request(*, item_id: int | None = None, search: str | None = None) -> OddsReferenceRequest:
    return OddsReferenceRequest(
        item_id=item_id,
        search=ValidatedOddsLabel(_clean_text(search, label="odds reference search", max_words=5, allow_single_fragment=True), _token=_COMPILER_TOKEN) if search else None,
    )


def _validate_optional_id(value: int | None, label: str) -> None:
    if value is not None:
        validate_positive_int(value, label)


def _require_any_id(**values: int | None) -> None:
    if not any(value is not None for value in values.values()):
        raise InvalidFootballApiRequest("request requires at least one validated ID.")
    for label, value in values.items():
        _validate_optional_id(value, label)


def _validate_id_batch(values: tuple[int, ...], label: str, *, max_value: int) -> None:
    if not isinstance(values, tuple):
        raise InvalidFootballApiRequest(f"{label} must be immutable.")
    if len(values) > max_value:
        raise InvalidFootballApiRequest(f"{label} supports at most {max_value} IDs.")
    seen: set[int] = set()
    for value in values:
        validate_positive_int(value, label)
        if value in seen:
            raise InvalidFootballApiRequest(f"{label} contains duplicate IDs.")
        seen.add(value)
