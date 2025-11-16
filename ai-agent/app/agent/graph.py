from typing import Any, Dict, TypedDict

from langgraph.graph import StateGraph

from app.agent.tools import journal_event


class AgentState(TypedDict):
    """
    Состояние, которое гуляет по графу.
    Пока минимальное: входящий апдейт, тип события и результат.
    """
    update: Dict[str, Any]
    event_kind: str
    result: Dict[str, Any]


def build_graph():
    def echo_and_journal_node(state: AgentState) -> AgentState:
        update = state["update"]
        kind = state.get("event_kind", "raw_update")

        # Пишем событие в журнал
        journal_event(update, kind=kind)

        # Пока агент просто возвращает echo-ответ.
        return {
            "update": update,
            "event_kind": kind,
            "result": {
                "type": "echo",
                "source": "langgraph",
                "event_kind": kind,
                "input": update,
            },
        }

    workflow = StateGraph(AgentState)
    workflow.add_node("echo_and_journal", echo_and_journal_node)
    workflow.set_entry_point("echo_and_journal")
    workflow.set_finish_point("echo_and_journal")

    return workflow.compile()


_graph = build_graph()


def run_agent(update: Dict[str, Any]) -> Dict[str, Any]:
    """
    Входная точка для FastAPI.
    Принимает update из webhook, гоняет через граф и возвращает result.
    Пока считаем все события как 'raw_update'.
    """
    initial_state: AgentState = {
        "update": update,
        "event_kind": "raw_update",
        "result": {},
    }
    final_state = _graph.invoke(initial_state)
    return final_state["result"]
