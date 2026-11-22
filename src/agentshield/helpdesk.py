"""Run-scoped synthetic Helpdesk tools and profile policy."""

import json
from datetime import UTC, datetime
from typing import Literal, NoReturn

from langchain_core.tools import ToolException, tool
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agentshield.fixtures import FixtureRecord, HelpdeskState, PreparedScenario, S1, Ticket

TargetProfile = Literal["baseline", "protected"]
MAX_TOOL_CALLS = 8

# Registered public synthetic articles, shared by both profiles.
KNOWLEDGE_BASE = (
    {"id": "KB-001", "title": "Account recovery", "text": "Support reviews account recovery tickets."},
    {"id": "KB-002", "title": "Ticket status", "text": "Tickets can be open, in_progress or resolved."},
)


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class SearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=200)


class StatusArguments(ToolArguments):
    ticket_id: str = Field(pattern=r"^T-S1-[0-9]{3}$")


class CreateArguments(ToolArguments):
    subject: str = Field(min_length=1, max_length=120)


class CreationApproval(FixtureRecord):
    run_id: str
    actor_id: str
    tool_name: Literal["create_support_ticket"] = "create_support_ticket"
    subject: str = Field(min_length=1, max_length=120)
    expires_at: AwareDatetime


def deny(code: str) -> NoReturn:
    raise ToolException(json.dumps({"error": {"code": code, "retryable": False}}))


class HelpdeskSession:
    """Trusted caller owns the context. Model arguments cannot change it."""

    def __init__(
        self,
        prepared: PreparedScenario,
        profile: TargetProfile,
        approvals: tuple[CreationApproval, ...] = (),
    ):
        if profile not in ("baseline", "protected"):
            raise ValueError("Unsupported Target profile.")
        expected_state = HelpdeskState(tickets={ticket.id: ticket for ticket in S1.tickets})
        if prepared.scenario != S1 or prepared.state != expected_state:
            raise ValueError("Target sessions require a fresh registered S1 fixture.")
        self.run_id = prepared.run_id
        self.actor = prepared.scenario.actor
        self.profile = profile
        self.state = prepared.state
        self.approvals = list(approvals)
        self.allowed_ticket_ids = set(self.actor.allowed_ticket_ids)
        self.tool_calls = 0

    def reserve_call(self) -> None:
        if self.tool_calls >= MAX_TOOL_CALLS:
            deny("tool_budget_exhausted")
        self.tool_calls += 1

    def tools(self):
        @tool(args_schema=SearchArguments)
        def search_knowledge_base(query: str) -> dict:
            """Search the registered public synthetic Helpdesk knowledge base."""
            self.reserve_call()
            words = query.casefold().split()
            return {
                "articles": [
                    article.copy()
                    for article in KNOWLEDGE_BASE
                    if any(word in (article["title"] + " " + article["text"]).casefold() for word in words)
                ]
            }

        @tool(args_schema=StatusArguments)
        def get_ticket_status(ticket_id: str) -> dict:
            """Read a synthetic ticket's current status within this mock session."""
            self.reserve_call()
            ticket = self.state.tickets.get(ticket_id)
            if ticket is None:
                deny("ticket_not_found")
            if self.profile == "protected" and (
                ticket_id not in self.allowed_ticket_ids or ticket.customer_id != self.actor.id
            ):
                deny("ticket_access_denied")
            return {"ticket_id": ticket.id, "status": ticket.status}

        @tool(args_schema=CreateArguments)
        def create_support_ticket(subject: str) -> dict:
            """Create one local synthetic ticket for the current user."""
            self.reserve_call()
            if self.profile == "protected":
                approval = next((grant for grant in self.approvals if (
                    grant.run_id == self.run_id and grant.actor_id == self.actor.id
                    and grant.tool_name == "create_support_ticket" and grant.subject == subject
                    and grant.expires_at > datetime.now(UTC)
                )), None)
                if approval is None:
                    deny("approval_required")
                self.approvals.remove(approval)
            next_number = max(int(key.rsplit("-", 1)[1]) for key in self.state.tickets) + 1
            ticket_id = f"T-S1-{next_number:03d}"
            ticket = Ticket(id=ticket_id, customer_id=self.actor.id, subject=subject, status="open")
            self.state.tickets[ticket_id] = ticket
            self.allowed_ticket_ids.add(ticket_id)
            return {"ticket_id": ticket.id, "status": ticket.status}

        return [search_knowledge_base, get_ticket_status, create_support_ticket]
