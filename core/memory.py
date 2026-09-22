from __future__ import annotations

import time
from typing import Any, Iterable

from core.storage import JSONStore


class Memory:
    def __init__(self, store: JSONStore) -> None:
        self.store = store

    def user(self, user_id: int | str) -> dict[str, Any]:
        users = self.store.section("users")
        key = str(user_id)
        profile = users.get(key)
        if profile is None:
            profile = {
                "id": key,
                "first_seen": time.time(),
                "names": [],
                "cases": [],
                "tickets": [],
                "notes": [],
                "filter_strikes": 0,
                "filter_history": [],
                "join_history": [],
                "blocked_from_tickets": False,
                "last_dm_at": None,
                "meta": {},
            }
            users[key] = profile
            self.store.mark_dirty()
        return profile

    def remember_name(self, user_id: int | str, name: str) -> None:
        profile = self.user(user_id)
        names = profile.setdefault("names", [])
        if not names or names[-1].get("name") != name:
            names.append({"name": name, "at": time.time()})
            del names[:-25]
            self.store.mark_dirty()

    def record_join(self, user_id: int | str, event: str) -> None:
        profile = self.user(user_id)
        history = profile.setdefault("join_history", [])
        history.append({"event": event, "at": time.time()})
        del history[:-40]
        self.store.mark_dirty()

    def add_note(self, user_id: int | str, author_id: int, text: str) -> dict[str, Any]:
        profile = self.user(user_id)
        note = {"author": str(author_id), "text": text, "at": time.time()}
        profile.setdefault("notes", []).append(note)
        self.store.mark_dirty()
        return note

    def set_ticket_block(self, user_id: int | str, blocked: bool) -> None:
        self.user(user_id)["blocked_from_tickets"] = blocked
        self.store.mark_dirty()

    def create_case(
        self,
        *,
        user_id: int,
        moderator_id: int,
        rule_id: str,
        clause_id: str,
        rule_title: str,
        clause_title: str,
        offense: int,
        action: str,
        duration: int | None,
        duration_label: str,
        reason: str,
        evidence: str | None = None,
        source: str = "command",
    ) -> dict[str, Any]:
        case_id = self.store.next_id("case")
        case = {
            "id": case_id,
            "user": str(user_id),
            "moderator": str(moderator_id),
            "rule": rule_id,
            "clause": clause_id,
            "rule_title": rule_title,
            "clause_title": clause_title,
            "offense": offense,
            "action": action,
            "duration": duration,
            "duration_label": duration_label,
            "reason": reason,
            "evidence": evidence,
            "source": source,
            "created_at": time.time(),
            "active": True,
            "revoked": False,
            "revoked_by": None,
            "revoked_at": None,
            "revoke_reason": None,
        }
        self.store.section("cases")[str(case_id)] = case
        self.user(user_id).setdefault("cases", []).append(case_id)
        self.store.bump_stat("cases_created")
        self.store.mark_dirty()
        return case

    def case(self, case_id: int | str) -> dict[str, Any] | None:
        return self.store.section("cases").get(str(case_id))

    def cases_for(self, user_id: int | str, include_revoked: bool = True) -> list[dict[str, Any]]:
        cases = self.store.section("cases")
        ids = self.user(user_id).get("cases", [])
        found = [cases[str(cid)] for cid in ids if str(cid) in cases]
        if not include_revoked:
            found = [c for c in found if not c.get("revoked")]
        return sorted(found, key=lambda c: c.get("created_at", 0))

    def offense_count(self, user_id: int | str, rule_id: str, clause_id: str) -> int:
        count = 0
        for case in self.cases_for(user_id, include_revoked=False):
            if case.get("rule") == rule_id and case.get("clause") == clause_id:
                count += 1
        return count

    def revoke_case(self, case_id: int | str, moderator_id: int, reason: str) -> dict[str, Any] | None:
        case = self.case(case_id)
        if case is None or case.get("revoked"):
            return None
        case["revoked"] = True
        case["active"] = False
        case["revoked_by"] = str(moderator_id)
        case["revoked_at"] = time.time()
        case["revoke_reason"] = reason
        self.store.bump_stat("cases_revoked")
        self.store.mark_dirty()
        return case

    def deactivate_case(self, case_id: int | str) -> None:
        case = self.case(case_id)
        if case is not None:
            case["active"] = False
            self.store.mark_dirty()

    def active_cases_of_action(self, user_id: int | str, action: str) -> list[dict[str, Any]]:
        return [
            c
            for c in self.cases_for(user_id, include_revoked=False)
            if c.get("action") == action and c.get("active")
        ]

    def create_ticket(
        self,
        *,
        user_id: int,
        channel_id: int,
        ticket_type: str,
        scope: str,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        ticket_id = self.store.next_id("ticket")
        now = time.time()
        ticket = {
            "id": ticket_id,
            "user": str(user_id),
            "channel": str(channel_id),
            "type": ticket_type,
            "scope": scope,
            "answers": answers,
            "status": "open",
            "created_at": now,
            "closed_at": None,
            "closed_by": None,
            "close_reason": None,
            "claimed_by": None,
            "claimed_at": None,
            "first_response_at": None,
            "first_responder": None,
            "last_member_at": now,
            "last_staff_at": None,
            "reminded_at": None,
            "on_hold": False,
            "rating": None,
            "rating_comment": None,
            "archive": None,
            "participants": [],
            "messages": [],
            "transcript": None,
        }
        self.store.section("tickets")[str(ticket_id)] = ticket
        self.store.section("open_by_user")[str(user_id)] = ticket_id
        self.store.section("channel_index")[str(channel_id)] = ticket_id
        self.user(user_id).setdefault("tickets", []).append(ticket_id)
        self.store.bump_stat("tickets_opened")
        self.store.mark_dirty()
        return ticket

    def ticket(self, ticket_id: int | str) -> dict[str, Any] | None:
        if ticket_id is None:
            return None
        return self.store.section("tickets").get(str(ticket_id))

    def ticket_by_channel(self, channel_id: int | str) -> dict[str, Any] | None:
        ticket_id = self.store.section("channel_index").get(str(channel_id))
        return self.ticket(ticket_id)

    def open_ticket_for(self, user_id: int | str) -> dict[str, Any] | None:
        ticket_id = self.store.section("open_by_user").get(str(user_id))
        if ticket_id is None:
            return None
        ticket = self.ticket(ticket_id)
        if ticket is None or ticket.get("status") != "open":
            self.store.section("open_by_user").pop(str(user_id), None)
            self.store.mark_dirty()
            return None
        return ticket

    def tickets_for(self, user_id: int | str) -> list[dict[str, Any]]:
        ids = self.user(user_id).get("tickets", [])
        found = [self.ticket(tid) for tid in ids]
        return [t for t in found if t is not None]

    def log_ticket_message(
        self,
        ticket_id: int | str,
        *,
        author_id: int,
        author_name: str,
        author_avatar: str | None,
        role: str,
        content: str,
        attachments: Iterable[dict[str, Any]] = (),
        internal: bool = False,
    ) -> dict[str, Any] | None:
        ticket = self.ticket(ticket_id)
        if ticket is None:
            return None
        entry = {
            "author": str(author_id),
            "author_name": author_name,
            "author_avatar": author_avatar,
            "role": role,
            "content": content,
            "attachments": list(attachments),
            "internal": internal,
            "at": time.time(),
        }
        ticket.setdefault("messages", []).append(entry)
        participants = ticket.setdefault("participants", [])
        if str(author_id) not in participants:
            participants.append(str(author_id))
        self.store.mark_dirty()
        return entry

    def close_ticket(
        self,
        ticket_id: int | str,
        *,
        closed_by: int,
        reason: str | None,
        transcript: str | None = None,
    ) -> dict[str, Any] | None:
        ticket = self.ticket(ticket_id)
        if ticket is None:
            return None
        ticket["status"] = "closed"
        ticket["closed_at"] = time.time()
        ticket["closed_by"] = str(closed_by)
        ticket["close_reason"] = reason
        if transcript:
            ticket["transcript"] = transcript
        self.store.section("open_by_user").pop(ticket["user"], None)
        self.store.section("channel_index").pop(ticket["channel"], None)
        self.store.bump_stat("tickets_closed")
        self.store.mark_dirty()
        return ticket

    def claim_ticket(self, ticket_id: int | str, staff_id: int) -> None:
        ticket = self.ticket(ticket_id)
        if ticket is not None:
            ticket["claimed_by"] = str(staff_id)
            ticket["claimed_at"] = time.time()
            self.store.mark_dirty()

    def open_tickets(self) -> list[dict[str, Any]]:
        return [t for t in self.store.section("tickets").values() if t.get("status") == "open"]

    def record_member_message(self, ticket_id: int | str) -> None:
        ticket = self.ticket(ticket_id)
        if ticket is None:
            return
        ticket["last_member_at"] = time.time()
        ticket["reminded_at"] = None
        self.store.mark_dirty()

    def record_staff_reply(self, ticket_id: int | str, staff_id: int) -> None:
        ticket = self.ticket(ticket_id)
        if ticket is None:
            return
        now = time.time()
        if not ticket.get("first_response_at"):
            ticket["first_response_at"] = now
            ticket["first_responder"] = str(staff_id)
        ticket["last_staff_at"] = now
        ticket["reminded_at"] = None
        self.store.mark_dirty()

    def mark_ticket_reminded(self, ticket_id: int | str) -> None:
        ticket = self.ticket(ticket_id)
        if ticket is not None:
            ticket["reminded_at"] = time.time()
            self.store.mark_dirty()

    def set_ticket_hold(self, ticket_id: int | str, held: bool) -> None:
        ticket = self.ticket(ticket_id)
        if ticket is not None:
            ticket["on_hold"] = held
            ticket["reminded_at"] = None
            self.store.mark_dirty()

    def set_ticket_archive(self, ticket_id: int | str, details: dict[str, Any]) -> None:
        ticket = self.ticket(ticket_id)
        if ticket is not None:
            ticket["archive"] = dict(details)
            self.store.mark_dirty()

    def rate_ticket(self, ticket_id: int | str, score: int) -> dict[str, Any] | None:
        ticket = self.ticket(ticket_id)
        if ticket is None or ticket.get("rating"):
            return None
        ticket["rating"] = int(score)
        ticket["rated_at"] = time.time()
        self.store.bump_stat("tickets_rated")
        self.store.mark_dirty()
        return ticket

    def comment_on_rating(self, ticket_id: int | str, comment: str) -> dict[str, Any] | None:
        ticket = self.ticket(ticket_id)
        if ticket is None or not ticket.get("rating"):
            return None
        ticket["rating_comment"] = comment
        self.store.mark_dirty()
        return ticket

    def snippets(self) -> dict[str, Any]:
        return self.store.section("snippets")

    def snippet(self, name: str) -> dict[str, Any] | None:
        return self.snippets().get(name.casefold())

    def save_snippet(self, name: str, text: str, author_id: int) -> dict[str, Any]:
        entry = {"name": name.casefold(), "text": text, "author": str(author_id), "at": time.time(), "uses": 0}
        self.snippets()[entry["name"]] = entry
        self.store.mark_dirty()
        return entry

    def delete_snippet(self, name: str) -> bool:
        removed = self.snippets().pop(name.casefold(), None) is not None
        if removed:
            self.store.mark_dirty()
        return removed

    def use_snippet(self, name: str) -> None:
        entry = self.snippet(name)
        if entry is not None:
            entry["uses"] = int(entry.get("uses", 0)) + 1
            self.store.mark_dirty()

    def seed_snippets(self, defaults: dict[str, str], author_id: int) -> None:
        if self.guild_setting("snippets_seeded"):
            return
        for name, text in defaults.items():
            if self.snippet(name) is None:
                self.save_snippet(name, text, author_id)
        self.set_guild_setting("snippets_seeded", True)

    def create_appeal(
        self,
        *,
        user_id: int,
        case_id: int | None,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        appeal_id = self.store.next_id("appeal")
        appeal = {
            "id": appeal_id,
            "user": str(user_id),
            "case": case_id,
            "answers": answers,
            "status": "pending",
            "created_at": time.time(),
            "decided_at": None,
            "decided_by": None,
            "decision_reason": None,
            "message": None,
        }
        self.store.section("appeals")[str(appeal_id)] = appeal
        self.store.section("appeal_by_user")[str(user_id)] = appeal_id
        self.user(user_id).setdefault("appeals", []).append(appeal_id)
        self.store.bump_stat("appeals_opened")
        self.store.mark_dirty()
        return appeal

    def appeal(self, appeal_id: int | str) -> dict[str, Any] | None:
        if appeal_id is None:
            return None
        return self.store.section("appeals").get(str(appeal_id))

    def appeals_for(self, user_id: int | str) -> list[dict[str, Any]]:
        ids = self.user(user_id).get("appeals", [])
        found = [self.appeal(aid) for aid in ids]
        return [a for a in found if a is not None]

    def pending_appeal_for(self, user_id: int | str) -> dict[str, Any] | None:
        appeal_id = self.store.section("appeal_by_user").get(str(user_id))
        appeal = self.appeal(appeal_id)
        if appeal is None or appeal.get("status") != "pending":
            return None
        return appeal

    def last_appeal_decision(self, user_id: int | str) -> dict[str, Any] | None:
        decided = [a for a in self.appeals_for(user_id) if a.get("decided_at")]
        if not decided:
            return None
        return max(decided, key=lambda a: a.get("decided_at", 0))

    def decide_appeal(
        self,
        appeal_id: int | str,
        *,
        status: str,
        staff_id: int,
        reason: str | None,
    ) -> dict[str, Any] | None:
        appeal = self.appeal(appeal_id)
        if appeal is None or appeal.get("status") != "pending":
            return None
        appeal["status"] = status
        appeal["decided_at"] = time.time()
        appeal["decided_by"] = str(staff_id)
        appeal["decision_reason"] = reason
        self.store.section("appeal_by_user").pop(appeal["user"], None)
        self.store.bump_stat(f"appeals_{status}")
        self.store.mark_dirty()
        return appeal

    def set_appeal_location(
        self,
        appeal_id: int | str,
        channel_id: int,
        message_id: int,
        *,
        dedicated: bool = False,
    ) -> None:
        appeal = self.appeal(appeal_id)
        if appeal is not None:
            appeal["channel"] = str(channel_id)
            appeal["message"] = str(message_id)
            appeal["dedicated_channel"] = dedicated
            self.store.mark_dirty()

    def appeal_by_message(self, message_id: int | str) -> dict[str, Any] | None:
        for appeal in self.store.section("appeals").values():
            if appeal.get("message") == str(message_id):
                return appeal
        return None

    def appeal_by_channel(self, channel_id: int | str) -> dict[str, Any] | None:
        for appeal in self.store.section("appeals").values():
            if appeal.get("channel") == str(channel_id) and appeal.get("status") == "pending":
                return appeal
        return None

    def claim_appeal(self, appeal_id: int | str, staff_id: int) -> dict[str, Any] | None:
        appeal = self.appeal(appeal_id)
        if appeal is None:
            return None
        appeal["claimed_by"] = str(staff_id)
        appeal["claimed_at"] = time.time()
        self.store.mark_dirty()
        return appeal

    def log_appeal_message(
        self,
        appeal_id: int | str,
        *,
        author_id: int,
        author_name: str,
        role: str,
        content: str,
        attachments: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any] | None:
        appeal = self.appeal(appeal_id)
        if appeal is None:
            return None
        entry = {
            "author": str(author_id),
            "author_name": author_name,
            "role": role,
            "content": content,
            "attachments": list(attachments),
            "at": time.time(),
        }
        appeal.setdefault("messages", []).append(entry)
        self.store.mark_dirty()
        return entry

    def schedule(
        self,
        *,
        kind: str,
        user_id: int,
        guild_id: int,
        expires_at: float,
        case_id: int | None,
    ) -> dict[str, Any]:
        entry = {
            "kind": kind,
            "user": str(user_id),
            "guild": str(guild_id),
            "expires_at": expires_at,
            "case": case_id,
            "created_at": time.time(),
        }
        self.store.data.setdefault("schedules", []).append(entry)
        self.store.mark_dirty()
        return entry

    def due_schedules(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        return [e for e in list(self.store.data.get("schedules", [])) if e.get("expires_at", 0) <= now]

    def drop_schedule(self, entry: dict[str, Any]) -> None:
        schedules = self.store.data.get("schedules", [])
        if entry in schedules:
            schedules.remove(entry)
            self.store.mark_dirty()

    def drop_schedules_for(self, user_id: int | str, kind: str) -> int:
        schedules = self.store.data.get("schedules", [])
        keep = [e for e in schedules if not (e.get("user") == str(user_id) and e.get("kind") == kind)]
        removed = len(schedules) - len(keep)
        if removed:
            self.store.data["schedules"] = keep
            self.store.mark_dirty()
        return removed

    def add_filter_strike(self, user_id: int | str, category: str, excerpt: str) -> int:
        profile = self.user(user_id)
        profile["filter_strikes"] = int(profile.get("filter_strikes", 0)) + 1
        history = profile.setdefault("filter_history", [])
        history.append({"category": category, "excerpt": excerpt[:300], "at": time.time()})
        del history[:-50]
        self.store.bump_stat("filter_hits")
        self.store.mark_dirty()
        return profile["filter_strikes"]

    def clear_filter_strikes(self, user_id: int | str) -> None:
        self.user(user_id)["filter_strikes"] = 0
        self.store.mark_dirty()

    def guild_setting(self, key: str, default: Any = None) -> Any:
        return self.store.section("guild").get(key, default)

    def set_guild_setting(self, key: str, value: Any) -> None:
        self.store.section("guild")[key] = value
        self.store.mark_dirty()

    def panel_state(self) -> dict[str, Any]:
        return self.store.section("panel")

    def set_panel_message(self, channel_id: int, message_id: int) -> None:
        panel = self.panel_state()
        panel["channel"] = str(channel_id)
        panel["message"] = str(message_id)
        panel["posted_at"] = time.time()
        self.store.mark_dirty()

    def clear_panel_message(self) -> None:
        panel = self.panel_state()
        panel.pop("message", None)
        self.store.mark_dirty()
