"""Durable Orchestrator.

Responsible for orchestrating the execution of the user defined generator
function.
"""
import logging
import traceback
from typing import Callable, Iterator, Any

from dateutil.parser import parse as dt_parse

from .interfaces import IFunctionContext
from .models import (
    DurableOrchestrationContext,
    Task,
    TaskSet,
    OrchestratorState)
from .models.history import HistoryEventType
from .tasks import should_suspend


class Orchestrator:
    """Durable Orchestration Class.

    Responsible for orchestrating the execution of the user defined generator
    function.
    """

    def __init__(self,
                 activity_func: Callable[[IFunctionContext], Iterator[Any]]):
        """Create a new orchestrator for the user defined generator.

        Responsible for orchestrating the execution of the user defined
        generator function.
        :param activity_func: Generator function to orchestrate.
        """
        self.fn: Callable[[IFunctionContext], Iterator[Any]] = activity_func
        self.customStatus: Any = None

    # noinspection PyAttributeOutsideInit
    def handle(self, context_string: str):
        """Handle the orchestration of the user defined generator function.

        Called each time the durable extension executes an activity and needs
        the client to handle the result.

        :param context_string: the context of what has been executed by
        the durable extension.
        :return: the resulting orchestration state, with instructions back to
        the durable extension.
        """
        self.durable_context = DurableOrchestrationContext(context_string)
        activity_context = IFunctionContext(df=self.durable_context)

        self.generator = self.fn(activity_context)
        suspended = False
        try:
            generation_state = self._generate_next(None)

            while not suspended:
                self._add_to_actions(generation_state)

                if should_suspend(generation_state):
                    orchestration_state = OrchestratorState(
                        is_done=False,
                        output=None,
                        actions=self.durable_context.actions,
                        custom_status=self.customStatus)
                    suspended = True
                    continue

                if (isinstance(generation_state, Task)
                    or isinstance(generation_state, TaskSet)) and (
                        generation_state.isFaulted):
                    generation_state = self.generator.throw(
                        generation_state.exception)
                    continue

                self._reset_timestamp()
                generation_state = self._generate_next(generation_state)

        except StopIteration as sie:
            orchestration_state = OrchestratorState(
                is_done=True,
                output=sie.value,
                actions=self.durable_context.actions,
                custom_status=self.customStatus)
        except Exception as e:
            e_string = traceback.format_exc()
            logging.warning(f"!!!Generator Termination Exception {e_string}")
            orchestration_state = OrchestratorState(
                is_done=False,
                output=None,  # Should have no output, after generation range
                actions=self.durable_context.actions,
                error=str(e),
                custom_status=self.customStatus)

        return orchestration_state.to_json_string()

    def _generate_next(self, partial_result):
        if partial_result is not None:
            gen_result = self.generator.send(partial_result.result)
        else:
            gen_result = self.generator.send(None)
        return gen_result

    def _add_to_actions(self, generation_state):
        if (isinstance(generation_state, Task)
                and hasattr(generation_state, "action")):
            self.durable_context.actions.append([generation_state._action])
        elif (isinstance(generation_state, TaskSet)
              and hasattr(generation_state, "actions")):
            self.durable_context.actions.append(generation_state.actions)

    def _reset_timestamp(self):
        last_timestamp = dt_parse(
            self.durable_context.decision_started_event['Timestamp'])
        decision_started_events = list(
            filter(lambda e_: (
                e_["EventType"] == HistoryEventType.OrchestratorStarted
                and dt_parse(e_["Timestamp"]) > last_timestamp),
                self.durable_context.histories))
        if len(decision_started_events) == 0:
            self.durable_context.current_utc_datetime = None
        else:
            self.durable_context.decision_started_event = \
                decision_started_events[0]
            self.durable_context.current_utc_datetime = dt_parse(
                self.durable_context.decision_started_event['Timestamp'])

    @classmethod
    def create(cls, fn):
        """Create an instance of the orchestration class.

        :param fn: Generator function that needs orchestration
        :return: Handle function of the newly created orchestration client
        """
        return lambda context: Orchestrator(fn).handle(context)
