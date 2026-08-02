"""Input parsing helpers."""


def parse_record(line: str) -> dict[str, str]:
    name, active = (part.strip() for part in line.split(",", maxsplit=1))
    return {"name": name, "active": active.lower()}
