from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from ..adapter import AgentTurn, HarnessPromptKind, RunMetadata

SCRIPTED_NAME = "scripted"
SCRIPTED_VERSION = "1.0.0"
SCRIPTED_CONTEXT_POLICY = "custom:scripted"
SCRIPTED_ATTEMPT_DEFINITION = (
    "one completed model turn in reply to the active call prompt or a reminder "
    "in which no terminal action for that call committed"
)


class ScriptExhausted(RuntimeError):
    pass


class ScriptedProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class SentPrompt:

    index: int
    kind: HarnessPromptKind
    text: str
    deadline_s: float
    service_date: str | None


@dataclass(frozen=True)
class ScriptedReply:

    text: str = ""
    ended_normally: bool = True
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    raises: BaseException | None = None
    hold_s: float = 0.0
    action: Callable[[SentPrompt], None] | None = None

    def turn(self) -> AgentTurn:
        return AgentTurn(
            text=self.text,
            ended_normally=self.ended_normally,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )


COMPLETED_EMPTY_REPLY = ScriptedReply()
INCOMPLETE_REPLY = ScriptedReply(text="noted")
PROVIDER_FAILURE_REPLY = ScriptedReply(raises=ScriptedProviderError("provider unavailable"))
TRUNCATED_REPLY = ScriptedReply(text="partial", ended_normally=False)
TIMED_OUT_REPLY = ScriptedReply(raises=TimeoutError("scripted turn exceeded its deadline"))


@dataclass
class ScriptedRun:

    workspace: Path | None = None
    system_prompt: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    days_started: list[str] = field(default_factory=list)
    days_ended: list[str] = field(default_factory=list)
    end_status: str | None = None
    prompts: list[SentPrompt] = field(default_factory=list)
    turns: list[AgentTurn] = field(default_factory=list)
    calls: list[tuple[str, object]] = field(default_factory=list)


class ScriptedAdapter:

    def __init__(
        self,
        replies: Sequence[ScriptedReply] | None = None,
        *,
        responder: Callable[[SentPrompt], ScriptedReply] | None = None,
        default_reply: ScriptedReply = COMPLETED_EMPTY_REPLY,
        model_id: str | None = None,
        name: str = SCRIPTED_NAME,
        version: str = SCRIPTED_VERSION,
        context_policy: str = SCRIPTED_CONTEXT_POLICY,
        attempt_definition: str = SCRIPTED_ATTEMPT_DEFINITION,
        strict: bool = False,
    ) -> None:
        self.name = name
        self.version = version
        self.context_policy = context_policy
        self.attempt_definition = attempt_definition
        self.model_id = model_id
        self.record = ScriptedRun()
        self._replies = list(replies or ())
        self._responder = responder
        self._default_reply = default_reply
        self._strict = strict
        self._current_day: str | None = None

    @property
    def prompts(self) -> list[SentPrompt]:
        return self.record.prompts

    @property
    def texts(self) -> list[str]:
        return [prompt.text for prompt in self.record.prompts]

    def prompts_of_kind(self, kind: HarnessPromptKind) -> list[SentPrompt]:
        return [prompt for prompt in self.record.prompts if prompt.kind == kind]

    def start_run(
        self, workspace: Path, system_prompt: str, env: Mapping[str, str]
    ) -> None:
        self.record.workspace = Path(workspace)
        self.record.system_prompt = system_prompt
        self.record.env = dict(env)
        self.record.calls.append(("start_run", str(workspace)))

    def start_day(self, service_date: date) -> None:
        self._current_day = service_date.isoformat()
        self.record.days_started.append(self._current_day)
        self.record.calls.append(("start_day", self._current_day))

    def end_day(self, service_date: date) -> None:
        self.record.days_ended.append(service_date.isoformat())
        self.record.calls.append(("end_day", service_date.isoformat()))
        self._current_day = None

    def end_run(self, status: str) -> None:
        self.record.end_status = status
        self.record.calls.append(("end_run", status))

    def describe(self) -> RunMetadata:
        return RunMetadata(
            name=self.name,
            version=self.version,
            context_policy=self.context_policy,
            attempt_definition=self.attempt_definition,
            model_id=self.model_id,
        )

    def send(self, prompt: str, kind: HarnessPromptKind, deadline_s: float) -> AgentTurn:
        sent = SentPrompt(
            index=len(self.record.prompts),
            kind=kind,
            text=prompt,
            deadline_s=float(deadline_s),
            service_date=self._current_day,
        )
        self.record.prompts.append(sent)
        self.record.calls.append(("send", kind))
        reply = self._reply_for(sent)
        if reply.action is not None:
            reply.action(sent)
        if reply.hold_s > 0.0:
            time.sleep(min(reply.hold_s, max(float(deadline_s), 0.0)))
            if reply.hold_s > float(deadline_s):
                raise TimeoutError(f"scripted turn exceeded deadline of {deadline_s} seconds")
        if reply.raises is not None:
            raise reply.raises
        turn = reply.turn()
        self.record.turns.append(turn)
        return turn

    def _reply_for(self, sent: SentPrompt) -> ScriptedReply:
        if self._responder is not None:
            return self._responder(sent)
        if sent.index < len(self._replies):
            return self._replies[sent.index]
        if self._strict:
            raise ScriptExhausted(
                f"no scripted reply for prompt {sent.index} of kind {sent.kind}"
            )
        return self._default_reply


def replies_for_call_attempts(count: int) -> list[ScriptedReply]:
    return [replace(INCOMPLETE_REPLY, text=f"working on it {n}") for n in range(1, count + 1)]


__all__ = [
    "COMPLETED_EMPTY_REPLY",
    "INCOMPLETE_REPLY",
    "PROVIDER_FAILURE_REPLY",
    "SCRIPTED_ATTEMPT_DEFINITION",
    "SCRIPTED_CONTEXT_POLICY",
    "SCRIPTED_NAME",
    "SCRIPTED_VERSION",
    "TIMED_OUT_REPLY",
    "TRUNCATED_REPLY",
    "ScriptExhausted",
    "ScriptedAdapter",
    "ScriptedProviderError",
    "ScriptedReply",
    "ScriptedRun",
    "SentPrompt",
    "replies_for_call_attempts",
]
