"""Calcul déterministe du prochain tag de release candidate."""

from __future__ import annotations

import re

Version = tuple[int, int, int]
BUMPS = ("patch", "minor", "major")


def tag_prefix(tag_format: str, module: str | None) -> str:
    """Partie fixe du tag, avant la version (sert à filtrer les tags existants)."""
    return tag_format.split("{version}")[0].format(module=module or "")


def format_tag(
    tag_format: str, rc_format: str, module: str | None, version: Version, rc: int
) -> str:
    rc_version = rc_format.format(version=_str(version), n=rc)
    return tag_format.format(module=module or "", version=rc_version)


def next_rc(
    existing_tags: list[str],
    tag_format: str,
    rc_format: str,
    module: str | None,
    bump: str = "patch",
) -> tuple[Version, int]:
    """Prochaine (version, numéro de RC).

    Si une RC est déjà en cours au-delà de la dernière version finale, on la continue
    (rc.N+1). Sinon on incrémente la dernière version finale selon `bump`, en rc.1.
    """
    latest_final, rcs = _scan(existing_tags, tag_format, rc_format, module, bump)
    if rcs:
        version = max(rcs)
        return version, rcs[version] + 1
    return _bump(latest_final, bump), 1


def next_final(
    existing_tags: list[str],
    tag_format: str,
    rc_format: str,
    module: str | None,
    bump: str = "patch",
) -> Version:
    """Version finale à poser : celle de la RC en cours, sinon la dernière finale incrémentée."""
    latest_final, rcs = _scan(existing_tags, tag_format, rc_format, module, bump)
    return max(rcs) if rcs else _bump(latest_final, bump)


def format_final_tag(tag_format: str, module: str | None, version: Version) -> str:
    return tag_format.format(module=module or "", version=_str(version))


def _scan(
    existing_tags: list[str], tag_format: str, rc_format: str, module: str | None, bump: str
) -> tuple[Version, dict[Version, int]]:
    """(dernière version finale, {version: dernier n° de RC} des RC au-delà de cette finale)."""
    if bump not in BUMPS:
        raise ValueError(f"bump doit valoir {', '.join(BUMPS)}")
    pattern = _tag_pattern(tag_format, rc_format, module)
    finals: list[Version] = []
    rcs: dict[Version, int] = {}
    for tag in existing_tags:
        match = pattern.fullmatch(tag)
        if not match:
            continue
        version = (int(match["major"]), int(match["minor"]), int(match["patch"]))
        if match["rc"] is None:
            finals.append(version)
        else:
            rcs[version] = max(rcs.get(version, 0), int(match["rc"]))
    latest_final = max(finals, default=(0, 0, 0))
    return latest_final, {v: n for v, n in rcs.items() if v > latest_final}


def latest_tag(
    existing_tags: list[str], tag_format: str, rc_format: str, module: str | None
) -> str | None:
    """Tag le plus récent (version la plus haute ; une finale passe devant ses RC)."""
    pattern = _tag_pattern(tag_format, rc_format, module)

    def order(match: re.Match[str]):
        version = (int(match["major"]), int(match["minor"]), int(match["patch"]))
        return version, match["rc"] is None, int(match["rc"] or 0)

    matches = [m for m in map(pattern.fullmatch, existing_tags) if m]
    return max(matches, key=order).group(0) if matches else None


def _tag_pattern(tag_format: str, rc_format: str, module: str | None) -> re.Pattern[str]:
    if not rc_format.startswith("{version}"):
        raise ValueError("rc_format doit commencer par {version}, par exemple {version}-rc.{n}")
    rc_suffix = re.escape(rc_format[len("{version}") :]).replace(re.escape("{n}"), r"(?P<rc>\d+)")
    version = rf"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:{rc_suffix})?"
    before, after = tag_format.split("{version}")
    return re.compile(re.escape(before.format(module=module or "")) + version + re.escape(after))


def _bump(version: Version, bump: str) -> Version:
    major, minor, patch = version
    if bump == "major":
        return major + 1, 0, 0
    if bump == "minor":
        return major, minor + 1, 0
    return major, minor, patch + 1


def _str(version: Version) -> str:
    return ".".join(map(str, version))
