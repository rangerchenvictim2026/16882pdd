"""单工作者任务队列：暂停、取消、进度与重启恢复。"""
import asyncio
import uuid
from collections import Counter

from .filtering import evaluate, sorted_products
from .models import Product, SearchFilters, timestamp
from .providers import HumanActionRequired, SourceUnavailable

ACTIVE = {"queued", "running", "paused", "cancelling"}


class TaskManager:
    def __init__(self, store, provider, item_interval=1.0):
        self.store, self.provider = store, provider
        self.item_interval = item_interval
        self.queue = asyncio.Queue()
        self.worker = None
        self.waiters = {}
        self.cancelled = set()
        self.current = None
        self.on_complete = None

    async def start(self):
        for task in self.store.tasks():
            if task["status"] in ACTIVE:
                task["status"] = "interrupted"
                task["message"] = "服务曾停止，已保留完成结果；请点击重试"
                self.store.put_task(task)
        self.worker = asyncio.create_task(self.run())

    def create(self, filters: SearchFilters, retry_of=None, refresh_id=None, compare_after=False):
        if len([t for t in self.store.tasks() if t["status"] in ACTIVE]) >= 10:
            raise ValueError("队列已满，请先完成或取消已有任务")
        task = {"id": str(uuid.uuid4()), "filters": filters.model_dump(), "status": "queued", "message": "等待采集",
                "created_at": timestamp(), "updated_at": timestamp(), "scanned": 0, "qualified": 0,
                "excluded": 0, "reasons": {}, "retry_of": retry_of, "refresh_id": refresh_id,
                "verification": "unverified", "sort_scope": "1688原生销量排序；未提供统计周期时不标为近30天" if filters.selection_mode == "platform" else "本次采集结果；件与笔分组，近30天销量展示值降序"}
        # 原生模式必须重新取得页面顺序；切换口径不继承旧任务的判断。
        original = self.store.task(retry_of) if retry_of else None
        reuse = original and original["filters"].get("selection_mode", "strict") == filters.selection_mode == "strict"
        if reuse:
            for item in self.store.task_items(retry_of, qualified_only=False):
                product = Product.model_validate(item["product"])
                reasons = evaluate(product, filters)
                if not reasons:
                    self.store.put_task(task)
                    self.store.put_product(product, task["id"], reasons)
        self.store.put_task(task)
        if compare_after:
            task['compare_after']=True
            self.store.put_task(task)
        self.recount(task)
        self.queue.put_nowait(task["id"])
        return self.store.task(task["id"])

    def recount(self, task):
        items = self.store.task_items(task["id"], qualified_only=False)
        task["scanned"] = len(items)
        task["qualified"] = sum(item["qualified"] for item in items)
        task["excluded"] = len(items) - task["qualified"]
        task["reasons"] = dict(Counter(reason for item in items for reason in item["reasons"]))
        self.store.put_task(task)

    def cancel(self, task_id):
        task = self.store.task(task_id)
        if not task or task["status"] not in ACTIVE:
            return task
        self.cancelled.add(task_id)
        task["status"] = "cancelled"
        task["message"] = "已取消，已完成结果已保留"
        self.store.put_task(task)
        if task_id in self.waiters:
            self.waiters[task_id].set()
        return task

    def resume(self, task_id):
        task = self.store.task(task_id)
        if not task or task["status"] != "paused" or task_id not in self.waiters:
            raise ValueError("此任务当前不处于可继续的暂停状态")
        task["message"] = "正在重新核查登录或验证状态"
        self.store.put_task(task)
        self.waiters[task_id].set()

    async def run(self):
        while True:
            task_id = await self.queue.get()
            self.current = task_id
            try:
                await self.execute(task_id)
            finally:
                self.current = None
                self.queue.task_done()

    async def execute(self, task_id):
        if task_id in self.cancelled:
            return
        task = self.store.task(task_id)
        filters = SearchFilters.model_validate(task["filters"])
        # 重试时复用仍有效的合格结果，重新核实被排除或已过期的商品。
        seen = {item["product"]["id"] for item in self.store.task_items(task_id, False)
                if item["qualified"] and not evaluate(Product.model_validate(item["product"]), filters)}
        while task_id not in self.cancelled:
            task = self.store.task(task_id)
            task["status"], task["message"] = "running", "正在逐件核实商品信息"
            self.store.put_task(task)
            try:
                if task.get("refresh_id"):
                    product = await self.provider.refresh(task["refresh_id"], filters) if hasattr(self.provider, "refresh") else await self.provider.detail(task["refresh_id"])
                    if task_id not in self.cancelled:
                        self.store.put_product(product, task_id, evaluate(product, filters))
                        self.recount(task)
                else:
                    async for product in self.provider.search(filters, seen):
                        if task_id in self.cancelled:
                            return
                        self.store.put_product(product, task_id, evaluate(product, filters))
                        self.recount(task)
                        await asyncio.sleep(self.item_interval)
                if task_id in self.cancelled:
                    return
                task["status"] = "completed"
                task["message"] = ("已保留平台筛选结果与原生顺序；未提供的天数、销量周期保持未知" if filters.selection_mode == "platform" else "采集已结束，结果仅代表本次获取范围；真实数据接入仍待验收") if task["qualified"] else "本次未取得符合当前口径的商品，请查看具体排除原因；字段未读取不代表货源一定不合格"
                self.store.put_task(task)
                if task.get('compare_after') and self.on_complete and task['qualified']:
                    try:
                        result=self.on_complete(task)
                        task['competition_id']=result['id']
                    except ValueError as error:
                        task['comparison_error']=str(error)
                    self.store.put_task(task)
                return
            except HumanActionRequired as error:
                if task_id in self.cancelled:
                    return
                task["status"], task["message"] = "paused", str(error)
                self.store.put_task(task)
                event = asyncio.Event()
                self.waiters[task_id] = event
                await event.wait()
                self.waiters.pop(task_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if task_id in self.cancelled:
                    return
                task["status"] = "failed"
                task["message"] = str(error) if isinstance(error, SourceUnavailable) else "采集发生异常，已保留完成结果；请检查浏览器、网络或重试"
                self.store.put_task(task)
                return

    def results(self, task_id):
        task = self.store.task(task_id)
        mode = task.get("filters", {}).get("selection_mode", "strict") if task else "strict"
        filters = SearchFilters.model_validate(task["filters"]) if task else None
        return sorted_products([i["product"] for i in self.store.task_items(task_id)
                                if filters is not None and not evaluate(Product.model_validate(i["product"]),filters)], mode)

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        await self.provider.close()
