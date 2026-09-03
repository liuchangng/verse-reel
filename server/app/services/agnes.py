"""Agnes AI API 客户端 - 支持每个模型独立的 API Key / Base URL"""
import logging
import asyncio
from typing import Optional
import httpx

from app.config import settings
from app.services.rate_limiter import (
    text_limiter,
    image_1k_limiter,
    image_high_limiter,
    video_limiter,
    get_image_limiter,
)

logger = logging.getLogger(__name__)


class AgnesClient:
    """Agnes AI API 客户端
    
    每个模型（文本/图片/视频）可以使用不同的供应商，
    因此每个模型有独立的 API Key 和 Base URL。
    """
    
    def __init__(self):
        # 占位：实际每请求动态读取（支持 settings.POST 热更新，无需重启）
        pass

    # ---- 类型化配置读取 ----
    @property
    def text_api_key(self) -> str:
        return settings.text_api_key

    @property
    def text_base_url(self) -> str:
        return settings.text_base_url

    @property
    def text_model(self) -> str:
        return settings.text_model

    @property
    def image_api_key(self) -> str:
        return settings.image_api_key

    @property
    def image_base_url(self) -> str:
        return settings.image_base_url

    @property
    def image_model(self) -> str:
        return settings.image_model

    @property
    def video_api_key(self) -> str:
        return settings.video_api_key

    @property
    def video_base_url(self) -> str:
        return settings.video_base_url

    @property
    def video_model(self) -> str:
        return settings.video_model
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        api_key: str = None,
        base_url: str = None,
        **kwargs
    ) -> dict:
        """发送 API 请求
        
        Args:
            method: HTTP 方法
            endpoint: API 端点
            api_key: API 密钥（不传则使用默认）
            base_url: Base URL（不传则使用默认）
        """
        url = f"{base_url or self.text_base_url}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {api_key or self.text_api_key}",
            "Content-Type": "application/json",
        }
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                **kwargs,
            )
            response.raise_for_status()
            return response.json()
    
    async def generate_text(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = 4000,
        temperature: float = 0.7,
    ) -> str:
        """生成文本"""
        model = model or self.text_model

        data = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # D4：主动节流（避免 RPM 撞限）。文本 18 RPM。
        await text_limiter.acquire()
        try:
            result = await self._request(
                "POST", "chat/completions",
                api_key=self.text_api_key,
                base_url=self.text_base_url,
                json=data,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                text_limiter.record_429()
            raise
        return result["choices"][0]["message"]["content"]
    
    async def generate_image(
        self,
        prompt: str,
        size: str = "1K",
        ratio: str = "1:1",
        model: Optional[str] = None,
        reference_image: Optional[str] = None,
    ) -> str:
        """生成图片
        
        Args:
            prompt: 图片描述
            size: 图片尺寸（SKILL: 1K/2K/3K/4K）
            ratio: 比例（1:1/16:9/9:16/4:3/3:4）
            model: 模型名称
            reference_image: 参考图 URL（图生图，放 extra_body.image）
        """
        model = model or self.image_model
        
        data = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "ratio": ratio,
            "n": 1,
            "extra_body": {
                "response_format": "url",
            },
        }
        
        # 图生图：参考图放 extra_body.image（SKILL 规范，非 mode/reference）
        if reference_image:
            data["extra_body"]["image"] = [reference_image]

        # D4：主动节流（图片 1K 18 / 2K+ 8）。
        await get_image_limiter(size).acquire()
        last_err = None
        # agnes 图片服务偶发 5xx（500/502/503/504）与 429 限流：指数退避重试，
        # 给外部服务从故障窗口恢复的时间，避免单张卡片 500 直接废掉整条流水线。
        for attempt in range(6):
            try:
                result = await self._request(
                    "POST", "images/generations",
                    api_key=self.image_api_key,
                    base_url=self.image_base_url,
                    json=data,
                )
                return result["data"][0]["url"]
            except httpx.HTTPStatusError as e:
                last_err = e
                if e.response.status_code in (429, 500, 502, 503, 504):
                    if e.response.status_code == 429:
                        get_image_limiter(size).record_429()
                    wait = min(2 ** attempt * 3, 20)
                    logger.warning(
                        f"图片生成 HTTP {e.response.status_code}，退避 {wait}s 重试 "
                        f"({attempt + 1}/6): {str(e)[:120]}"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise
        raise last_err or RuntimeError("图片生成失败")
    
    async def generate_video(
        self,
        prompt: str,
        image_url: Optional[str] = None,
        width: int = 1152,
        height: int = 768,
        num_frames: int = 121,
        frame_rate: int = 24,
        model: Optional[str] = None,
        max_retries: int = 6,
    ) -> str:
        """生成视频（异步任务，agnes-video-v2.0）
        
        请求体严格按 SKILL 规范：width/height/num_frames(8n+1)/frame_rate；
        图生视频传顶层 image 字段。不接受 mode/seconds/size/aspect_ratio。
        
        限流：agnes 视频接口硬限流 1 次/分钟（rate_limit_exceeded，返回 429 或 400）。
        提交遇限流自动退避 ≥60s 重试，直至成功或超出重试次数。
        """
        model = model or self.video_model
        
        data = {
            "model": model,
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_frames": num_frames,
            "frame_rate": frame_rate,
        }
        
        # 图生视频：顶层 image 字段（SKILL 规范）
        if image_url:
            data["image"] = image_url

        last_err = None
        for attempt in range(max_retries):
            # D4：主动节流（视频 1 RPM = 每分钟 1 次）。
            # 与队列层 video_min_interval=62s 双保险：第一道关 (这里)
            # 防止同进程内多个 video Job 同时进来；第二道关 (队列层)
            # 保证跨任务跨重启的最小提交间隔。
            acquired = await video_limiter.acquire(timeout=120)
            if not acquired:
                # 120s 都排不上就抛错让外层重试
                raise RuntimeError(
                    f"视频限流等待超过 120s，请检查上游服务或减少触发频率"
                )
            try:
                result = await self._request(
                    "POST", "videos",
                    api_key=self.video_api_key,
                    base_url=self.video_base_url,
                    json=data,
                )
                return result.get("video_id") or result.get("id")
            except httpx.HTTPStatusError as e:
                # 限流可能以 429 或 400(rate_limit_exceeded) 返回
                is_rate_limit = e.response.status_code in (429, 400)
                try:
                    body = e.response.json()
                    if isinstance(body, dict) and body.get("error", {}).get("code") == "rate_limit_exceeded":
                        is_rate_limit = True
                except Exception:
                    pass
                if is_rate_limit:
                    video_limiter.record_429()
                    if attempt < max_retries - 1:
                        wait = 65 + attempt * 5
                        logger.warning(f"视频提交限流(1次/分钟)，退避 {wait}s 后重试 ({attempt + 1}/{max_retries})...")
                        await asyncio.sleep(wait)
                        last_err = e
                        continue
                raise
        raise last_err or RuntimeError("视频提交失败")
    
    async def poll_video(
        self,
        video_id: str,
        model: Optional[str] = None,
        max_wait: int = 420,
        interval: int = 12,
    ) -> dict:
        """轮询视频生成状态
        
        间隔 ≥10s（SKILL 要求），避免过快轮询触发 429 并发限流。
        """
        model = model or self.video_model
        
        for _ in range(max_wait // interval):
            try:
                result = await self._request(
                    "GET",
                    "agnesapi",
                    api_key=self.video_api_key,
                    base_url=self.video_base_url,
                    params={"video_id": video_id, "model_name": model}
                )
                
                status = result.get("status", "").lower()
                if status in {"completed", "succeeded", "done"}:
                    return result
                elif status in {"failed", "error"}:
                    raise RuntimeError(f"视频生成失败: {result}")
                
                logger.info(f"视频生成中... 状态: {status}")
                await asyncio.sleep(interval)
                
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    # 并发限流：放慢节奏退避，过一会儿即可恢复
                    logger.warning("视频轮询触发限速(429)，退避 20s 后继续...")
                    await asyncio.sleep(20)
                else:
                    raise
        
        raise TimeoutError(f"视频生成超时: {video_id}")


# 全局客户端实例
agnes_client = AgnesClient()
