from typing import Any, Optional

class CircularQueue:
    def __init__(self, size: int):
        self.size = size
        self.queue: list[Optional[Any]] = [None] * size
        self.front: int = -1
        self.rear: int = -1

    def isFull(self) -> bool:
        return self.rear == self.size - 1

    def isEmpty(self) -> bool:
        return self.front == -1 or self.front > self.rear

    def enqueue(self, item: Any) -> None:
        if self.isFull():
            return
        if self.isEmpty():
            self.front = self.rear = 0
        else:
            self.rear += 1
        self.queue[self.rear] = item

    def dequeue(self) -> Optional[Any]:
        if self.isEmpty():
            return None
        temp = self.queue[self.front]
        self.front += 1
        if self.front > self.rear:
            self.front = self.rear = -1
        return temp


class TreeNode:
    def __init__(
        self,
        ID: int,
        CurrentNode: str,
        Description: str,
        has_brother_node: bool,
        has_son_node: bool,
        parent: Optional["TreeNode"] = None,
        depth: int = 0,
    ):
        self.ID = ID
        self.CurrentNode = CurrentNode
        self.Description = Description
        self.has_brother_node = has_brother_node
        self.has_son_node = has_son_node
        self.parent = parent
        self.depth = depth
        self.BrotherNode: Optional["TreeNode"] = None
        self.SonNode: Optional["TreeNode"] = None
