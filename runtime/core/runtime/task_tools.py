# -*- coding: utf-8 -*-
"""任务图工具：让 Agent 创建、认领、完成持久化任务。"""
from __future__ import annotations

import json
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from core.runtime.tasks import Task, TaskStore

ICONS = {"pending": "○", "in_progress": "●", "completed": "✓", "failed": "✗"}


def _line(task: Task) -> str:
    icon = ICONS.get(task.status, "?")
    deps = f" blocked_by={task.blocked_by}" if task.blocked_by else ""
    owner = f" owner={task.owner}" if task.owner else ""
    return f"{icon} {task.id}: {task.subject} [{task.status}]{deps}{owner}"


def build_task_tools(store: TaskStore):
    @tool("create_task", description="创建一个持久化任务，可选 blocked_by 依赖。")
    def create_task(
        subject: Annotated[str, Field(description="任务标题，一句话说明要做什么")],
        description: Annotated[str, Field(description="任务详情，可写验收标准或补充背景")] = "",
        blocked_by: Annotated[
            list[str] | None,
            Field(description="依赖的任务 id 列表；这些任务完成前本任务不能被认领"),
        ] = None,
    ) -> str:
        task = store.create(subject, description, blocked_by)
        deps = f"，依赖 {blocked_by}" if blocked_by else ""
        return f"已创建 {task.id}: {task.subject}{deps}"

    @tool("list_tasks", description="列出所有任务、状态、owner 和依赖。")
    def list_tasks(
        status: Annotated[
            str,
            Field(description="只看某个状态的任务，如 pending / in_progress / completed / failed；留空列出全部"),
        ] = "",
    ) -> str:
        tasks = store.list(status or None)
        if not tasks:
            return "当前没有任务。"
        return "\n".join(_line(task) for task in tasks)

    @tool("get_task", description="查看指定任务的完整详情。")
    def get_task(
        task_id: Annotated[str, Field(description="要查看的任务 id，例如 task_1")],
    ) -> str:
        try:
            task = store.get(task_id)
        except KeyError as exc:
            return str(exc)
        return json.dumps(task.__dict__, ensure_ascii=False, indent=2)

    @tool("claim_task", description="认领一个 pending 且依赖已完成的任务。")
    def claim_task(
        task_id: Annotated[str, Field(description="要认领的任务 id；必须是 pending 且依赖已完成")],
    ) -> str:
        try:
            task = store.claim(task_id)
        except (KeyError, ValueError) as exc:
            return str(exc)
        return f"已认领 {task.id}: {task.subject}，owner={task.owner}"

    @tool("complete_task", description="完成一个 in_progress 任务，并返回被解锁的任务。")
    def complete_task(
        task_id: Annotated[str, Field(description="要标记完成的 in_progress 任务 id")],
        result: Annotated[str, Field(description="完成结果摘要，会记入任务记录")] = "",
    ) -> str:
        try:
            unblocked = store.complete(task_id, result)
        except (KeyError, ValueError) as exc:
            return str(exc)
        message = f"已完成 {task_id}"
        if unblocked:
            message += "；解锁：" + ", ".join(f"{task.id}:{task.subject}" for task in unblocked)
        return message

    @tool("fail_task", description="把一个 in_progress 任务标记为失败。")
    def fail_task(
        task_id: Annotated[str, Field(description="要标记失败的 in_progress 任务 id")],
        error: Annotated[str, Field(description="失败原因，会记入任务记录")] = "",
    ) -> str:
        try:
            task = store.fail(task_id, error)
        except KeyError as exc:
            return str(exc)
        return f"已标记失败 {task.id}: {error or '未提供原因'}"

    return [create_task, list_tasks, get_task, claim_task, complete_task, fail_task]
