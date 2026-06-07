from __future__ import annotations

import csv
import re
from pathlib import Path


def normalize_title(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


class QuartileLookup:
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self.by_issn: dict[str, str] = {}
        self.by_title: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        self.by_issn.clear()
        self.by_title.clear()
        if not self.csv_path.exists():
            return

        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                quartile = (
                    row.get("sci_quartile")
                    or row.get("quartile")
                    or row.get("jcr_quartile")
                    or row.get("中科院分区")
                    or ""
                ).strip()
                if not quartile:
                    continue

                for key in ("issn_l", "issn", "ISSN", "ISSN-L"):
                    raw = row.get(key)
                    if raw:
                        for issn in raw.replace(";", ",").split(","):
                            cleaned = issn.strip().upper()
                            if cleaned:
                                self.by_issn[cleaned] = quartile

                for key in ("journal_title", "journal", "source_title", "期刊名称"):
                    title = normalize_title(row.get(key))
                    if title:
                        self.by_title[title] = quartile

    def lookup(self, journal_title: str | None, issn_l: str | None, issns: list[str] | None) -> str:
        candidates = []
        if issn_l:
            candidates.append(issn_l)
        candidates.extend(issns or [])
        for issn in candidates:
            quartile = self.by_issn.get(issn.strip().upper())
            if quartile:
                return quartile

        title = normalize_title(journal_title)
        if title:
            return self.by_title.get(title, "未匹配")
        return "未配置"
