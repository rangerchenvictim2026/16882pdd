"""串行关键词竞品任务，独立保存货源快照、暂停状态和已完成结果。"""
import asyncio
import re
import uuid
from collections import Counter

from .competition_models import CompetitionItem, CompetitionJob
from .competition_ranking import rank_competitors
from .models import Product, timestamp
from .providers import HumanActionRequired, SourceUnavailable

ACTIVE = {"queued", "running", "paused"}


def search_keyword(title: str) -> str:
    text = re.sub(r"一件代发|厂家直供|源头工厂|工厂直销|跨境专供|批发|现货", "", title)
    return re.sub(r"\s+", " ", text).strip()[:80] or title.strip()[:80]


class CompetitionManager:
    def __init__(self, store, provider, source_tasks):
        self.store, self.provider, self.source_tasks = store, provider, source_tasks
        self.queue = asyncio.Queue()
        self.worker = self.operation = None
        self.current = None
        self.waiters = {}

    def event(self,job,message):
        events=job.setdefault('events',[])
        if not events or events[-1]['message']!=message:
            events.append({'time':timestamp(),'message':message})
            job['events']=events[-200:]
        self.store.put_competition(job)

    async def start(self):
        for job in self.store.competitions():
            if job["status"] in ACTIVE:
                job["status"], job["message"] = "interrupted", "服务曾停止，已保留完成结果；请重新检索"
                self.store.put_competition(job)
        self.worker = asyncio.create_task(self.run())

    def create(self, request):
        task = self.store.task(request.source_task_id)
        if not task:
            raise ValueError("货源任务不存在")
        if task["status"] in {"queued", "running", "paused", "cancelling"}:
            raise ValueError("请先完成或取消1688采集，再检索当前结果前5件")
        for job in self.store.competitions(request.source_task_id):
            if job["status"] in ACTIVE:
                return job
        if sum(job["status"] in ACTIVE for job in self.store.competitions()) >= 3:
            raise ValueError("竞品任务队列已满，请先完成或取消已有任务")
        products = self.source_tasks.results(request.source_task_id)[:5]
        if not products:
            raise ValueError("当前没有可用于竞品搜索的货源结果")
        if set(request.keywords) - {p["id"] for p in products}:
            raise ValueError("关键词只能对应当前结果前5件商品")
        items = [CompetitionItem(source=Product.model_validate(p), method="title",
                                 keyword=request.keywords.get(p["id"], search_keyword(p["title"])).strip()) for p in products]
        job = CompetitionJob(id=str(uuid.uuid4()), source_task_id=request.source_task_id,
                             status="queued", message="等待关键词检索", created_at=timestamp(),
                             updated_at=timestamp(), items=items).model_dump()
        self.store.put_competition(job)
        self.queue.put_nowait(job["id"])
        return job

    def cancel(self, job_id):
        job = self.store.competition(job_id)
        if job["status"] in ACTIVE:
            job["status"], job["message"] = "cancelled", "已取消，已完成的竞品结果已保留"
            for item in job["items"]:
                if item["status"] in {"pending", "running"}:
                    item["status"], item["message"] = "cancelled", "本件检索未完成，已取消"
            self.store.put_competition(job)
            if self.current == job_id and self.operation:
                self.operation.cancel()
        return job

    def resume(self, job_id):
        job = self.store.competition(job_id)
        if job['status']=='interrupted':
            job['status']='queued'
            job['message']='等待从保存的断点继续'
            self.event(job,job['message'])
            self.queue.put_nowait(job_id)
            return job
        if job["status"] != "paused" or job_id not in self.waiters:
            raise ValueError("此竞品任务当前不能继续，请重新检索")
        self.waiters[job_id].set()
        return job

    async def run(self):
        while True:
            job_id = await self.queue.get()
            self.current = job_id
            try:
                self.operation = asyncio.create_task(self.execute(job_id))
                try:
                    await self.operation
                except asyncio.CancelledError:
                    if self.store.competition(job_id)["status"] != "cancelled":
                        raise
            finally:
                if hasattr(self.provider, "progress"):
                    self.provider.progress = None
                self.waiters.pop(job_id, None)
                self.current = self.operation = None
                self.queue.task_done()

    async def execute(self, job_id):
        job = self.store.competition(job_id)
        if job["status"] == "cancelled":
            return
        for index, item in enumerate(job["items"]):
            if item['status'] in {'completed','no_results','insufficient_data'}:
                continue
            while True:
                job["status"], job["current_index"], job["action"] = "running", index, None
                job["message"] = f"正在检索第 {index + 1} / {len(job['items'])} 件货源"
                item["status"], item["message"] = "running", "正在读取拼多多搜索结果"
                self.store.put_competition(job)
                self.event(job,job['message'])
                def progress(message):
                    if self.store.competition(job_id)["status"] not in ACTIVE:
                        return
                    job["message"] = f"正在检索第 {index + 1} / {len(job['items'])} 件货源 · {message}"
                    item["message"] = message
                    self.event(job,message)
                if hasattr(self.provider, "progress"):
                    self.provider.progress = progress
                if hasattr(self.provider,'checkpoint'):
                    self.provider.checkpoint=item.get('checkpoint')
                    def checkpoint(value):
                        if self.store.competition(job_id)['status'] in ACTIVE:
                            item['checkpoint']=value
                            self.store.put_competition(job)
                    self.provider.save_checkpoint=checkpoint
                try:
                    batch = await self.provider.search_by_title(item["keyword"])
                    if not batch.candidates and not batch.empty_verified and not batch.scanned_count:
                        raise SourceUnavailable("未能确认搜索结果，请检查拼多多页面；不能据此判断无竞品")
                    matches, reasons = rank_competitors(batch.candidates)
                    excluded=dict(Counter(batch.excluded)+Counter(reasons))
                    count=batch.scanned_count if batch.scanned_count is not None else len(batch.candidates)
                    item.update(candidates_count=count, search_url=batch.search_url,
                                matches=[m.model_dump() for m in matches], excluded=excluded,
                                status="completed" if matches else "insufficient_data" if count else "no_results",
                                message=f"已读取 {count} 件，形成 {len(matches)} 条竞品结果" if count else "页面明确显示没有搜索结果")
                    self.event(job,item['message'])
                    break
                except HumanActionRequired as error:
                    event = asyncio.Event()
                    self.waiters[job_id] = event
                    job["status"], job["message"] = "paused", str(error)
                    job["action"] = "login" if "登录" in str(error) else "verification"
                    self.event(job,str(error))
                    await event.wait()
                    self.waiters.pop(job_id, None)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    job["status"] = item["status"] = "failed"
                    job["message"] = item["message"] = str(error) if isinstance(error, SourceUnavailable) else "竞品检索发生异常，已保留完成结果；请查看浏览器后重试"
                    self.store.put_competition(job)
                    return
            if index + 1 < len(job["items"]):
                await asyncio.sleep(1)
        job["status"], job["current_index"], job["action"] = "completed", len(job["items"]), None
        count = sum(len(item["matches"]) for item in job["items"])
        job["message"] = f"已检索 {len(job['items'])} 件货源，共取得 {count} 条可比较的竞品结果"
        self.store.put_competition(job)

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        await self.provider.close()
