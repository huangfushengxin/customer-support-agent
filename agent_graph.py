"""用 LangGraph 组织一轮处理流程，复用已有校验、工具和确认逻辑。"""
import copy
from dataclasses import asdict
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

import agent_service


class AgentState(TypedDict, total=False):
    # State 保存节点之间传递的数据。每个节点只返回需要更新的字段。
    user_text: str
    session: dict
    messages: list[dict]
    prepared: list
    documents: dict
    draft: dict | None
    trace: list[dict]
    flow_nodes: list[str]
    result: dict
    draft_token: str | None


def plan_node(state: AgentState):
    """节点一：模型选择工具，Python 先校验整批调用。"""
    updates = agent_service.plan_turn(state)
    return {**updates, "flow_nodes": state["flow_nodes"] + ["plan"]}


def choose_next_node(state: AgentState) -> Literal["tools", "reply"]:
    """条件边：本轮需要工具时执行工具，否则直接整理追问。"""
    return "tools" if state["prepared"] else "reply"


def tools_node(state: AgentState):
    """节点二：执行知识检索或草稿整理，收集工具返回值。"""
    updates = agent_service.execute_turn_tools(state)
    return {**updates, "flow_nodes": state["flow_nodes"] + ["tools"]}


def reply_node(state: AgentState):
    """节点三：生成最终回复，校验资料来源并提供确认命令。"""
    updates = agent_service.compose_turn_reply(state)
    # 在最后一个节点中提交会话快照，检查点才能保存本轮成功后的记忆。
    # 使用副本，失败时不会改变调用方正在使用的会话。
    session = agent_service.AgentSession(**copy.deepcopy(state["session"]))
    agent_service.commit_turn(session, {**state, **updates})
    return {**updates, "session": asdict(session),
            "flow_nodes": state["flow_nodes"] + ["reply"]}


def build_graph(*, checkpointer=None):
    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("tools", tools_node)
    builder.add_node("reply", reply_node)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", choose_next_node,
                                  {"tools": "tools", "reply": "reply"})
    builder.add_edge("tools", "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=checkpointer)


AGENT_GRAPH = build_graph()


def run_graph_agent(user_text, session=None):
    if session is None:
        session = agent_service.AgentSession()
    initial = agent_service.prepare_turn(user_text, session)
    # 不自动重试节点，避免无意中重复调用收费的模型接口。
    completed = AGENT_GRAPH.invoke(initial)
    # 最终节点已提交一次历史，这里只同步，避免同一轮被重复追加。
    restored = copy.deepcopy(completed["session"])
    session.history = restored["history"]
    session.draft = restored["draft"]
    session.draft_token = restored["draft_token"]
    session.saved_ticket_id = restored["saved_ticket_id"]
    return {**completed["result"], "flow_nodes": completed["flow_nodes"]}


if __name__ == "__main__":
    print("当前使用 LangGraph 流程：plan → tools（按需）→ reply")
    agent_service.main(run_turn=run_graph_agent)
