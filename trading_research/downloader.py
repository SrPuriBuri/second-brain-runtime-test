"""Checkpointed GET pagination; completed pages survive an interrupted chunk."""

from trading_runtime.config import SafetyError
from trading_runtime.market_calendar import NY, session_from_row
from .archive import atomic_json, chunks, normalize_bar, now, require_retention
from .corporate_actions import normalize_alpaca
from .data_quality import audit_bars, pre_oos_interval, utc_stamp
from .dataset_manifest import sha256


class Downloader:
    def __init__(self, archive, provider, retention):
        require_retention(retention)  # Before any provider call or credential use.
        self.archive, self.provider = archive, provider
        atomic_json(archive.path / "retention.json", retention)

    def page(self, route, params):
        index = self.archive.index()
        if route not in {"bars", "calendar", "corporate_actions", "assets"}:
            raise SafetyError("ARCHIVE_ROUTE_FORBIDDEN")
        if route in {"bars", "calendar", "corporate_actions"}:
            try:
                pre_oos_interval(params["start"], params["end"])
            except SafetyError:
                index["blocked_oos_requests"] += 1
                self.archive.checkpoint(index)
                raise
        if route == "bars" and (
            params.get("feed") != "sip" or params.get("adjustment") != "raw"
        ):
            raise SafetyError("ARCHIVE_PRIMARY_RAW_SIP_ONLY")
        identity = sha256({"route": route, "params": params})
        if identity in index["requests"]:
            return self.archive.read_object(index["requests"][identity])["body"]
        before = len(self.provider.receipts)
        try:
            body = self.provider.get(route, params)
            if route == "bars":
                for symbol, rows in (body.get("bars") or {}).items():
                    if symbol not in params["symbols"].split(","):
                        raise SafetyError("ARCHIVE_UNEXPECTED_SYMBOL")
                    for row in rows:
                        stamp = utc_stamp(row["t"])
                        if stamp.year >= 2025:
                            raise SafetyError("ARCHIVE_OOS_ROW_REJECTED")
                        if (
                            not utc_stamp(params["start"])
                            <= stamp
                            <= utc_stamp(params["end"])
                        ):
                            raise SafetyError("ARCHIVE_ROW_OUTSIDE_REQUEST")
            payload = {
                "request_identity": identity,
                "route": route,
                "parameters": params,
                "retrieved_at": now(),
                "body": body,
                "content_hash": sha256(body),
            }
            domain = (
                "market"
                if route == "bars"
                else "corporate_actions"
                if route == "corporate_actions"
                else "calendar"
                if route == "calendar"
                else "security_master"
            )
            index["requests"][identity] = self.archive.save_object(
                domain, identity, payload
            )
            return body
        finally:
            index["receipts"].extend(self.provider.receipts[before:])
            self.archive.checkpoint(index)
            # Bound in-memory page retention; the archive is now authoritative.
            self.provider.memo.clear()

    def pages(self, route, params, field):
        combined, seen, query = {}, set(), dict(params)
        for _ in range(100):
            body = self.page(route, query)
            for key, values in (body.get(field) or {}).items():
                combined.setdefault(key, []).extend(values)
            cursor = body.get("next_page_token")
            if not cursor:
                return combined
            if cursor in seen:
                raise SafetyError("ARCHIVE_PAGINATION_LOOP")
            seen.add(cursor)
            query["page_token"] = cursor
        raise SafetyError("ARCHIVE_PAGINATION_INCOMPLETE")

    def references(self):
        spec = self.archive.plan["selection"]
        calendar = self.page(
            "calendar", {"start": spec["archive_start"], "end": spec["archive_end"]}
        )
        raw = self.pages(
            "corporate_actions",
            {
                "symbols": ",".join(spec["symbols"]),
                "start": spec["archive_start"],
                "end": spec["archive_end"],
                "data_quality": "all",
                "limit": 1000,
                "sort": "asc",
            },
            "corporate_actions",
        )
        # Reuse native request retrieval provenance, never fabricate known_at.
        normalized = [
            normalize_alpaca(kind, row, now())
            for kind, rows in raw.items()
            for row in rows
        ]
        assets = self.page("assets", {"status": "active", "asset_class": "us_equity"})
        return calendar, normalized, assets

    def run(self, max_chunks=10):
        with self.archive.writer():
            return self._run(max_chunks)

    def _run(self, max_chunks):
        if not 1 <= max_chunks <= 2000:
            raise SafetyError("ARCHIVE_CHUNK_BUDGET_INVALID")
        calendar, actions, assets = self.references()
        completed = 0
        for chunk in chunks(self.archive.plan):
            index = self.archive.index()
            if chunk["id"] in index["chunks"]:
                self.archive.read_object(index["chunks"][chunk["id"]])
                continue
            params = chunk["params"]
            rows = self.pages("bars", params, "bars").get(params["symbols"], [])
            sessions = [
                r
                for r in calendar
                if params["start"][:10] <= r["date"] <= params["end"][:10]
            ]
            quality = audit_bars(
                {params["symbols"]: rows}, sessions, [params["symbols"]]
            )
            by_day = {r["date"]: session_from_row(r) for r in sessions}
            regular = []
            for row in rows:
                stamp = utc_stamp(row["t"])
                session = by_day.get(stamp.astimezone(NY).date().isoformat())
                if session and session.open <= stamp < session.close:
                    regular.append(normalize_bar(row))
            payload = {
                "status": "COMPLETE",
                "chunk": chunk,
                "rows": regular,
                "quality": quality,
                "row_count": len(regular),
                "retrieved_at": now(),
                "raw_observations": "request objects retain all provider fields and out-of-session observations",
            }
            index = self.archive.index()
            index["chunks"][chunk["id"]] = self.archive.save_object(
                "market", chunk["id"], payload
            )
            self.archive.checkpoint(index)
            completed += 1
            if completed >= max_chunks:
                break
        return {
            **self.archive.status(),
            "new_completed_chunks": completed,
            "corporate_actions": len(actions),
            "current_assets": len(assets),
        }


def offline_references(archive):
    calendar, actions, assets = [], [], []
    for ref in archive.index()["requests"].values():
        value = archive.read_object(ref)
        if value["route"] == "calendar":
            calendar.extend(value["body"])
        elif value["route"] == "assets":
            assets.extend(value["body"])
        elif value["route"] == "corporate_actions":
            for kind, rows in value["body"].get("corporate_actions", {}).items():
                actions.extend(
                    normalize_alpaca(kind, r, value["retrieved_at"]) for r in rows
                )
    return calendar, actions, assets
