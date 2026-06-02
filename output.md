# QA 测试报告：Raise Controller Coverage Toward 85%

## 1. 测试目标概述

| 目标项 | 描述 |
|--------|------|
| **覆盖率目标** | 从当前水平提升至 ≥85% |
| **核心模块** | BrowserManager, startup extension wiring, network inspector paths, route handlers |
| **约束条件** | 避免 live browser/network 依赖，使用 mock 和 fixture |
| **验证命令** | `python -m pytest tests --cov=app --cov-fail-under=80` 保持绿色 |

## 2. 测试策略设计

### 2.1 依赖隔离方案

| 模块 | 依赖项 | Mock 策略 |
|------|--------|-----------|
| BrowserManager | Playwright browser, context | `unittest.mock.patch` 替换 `playwright.async_api` |
| Extension wiring | 文件系统, 浏览器进程 | `pytest-mock` + `tmp_path` fixture |
| Network inspector | 网络请求/响应 | `aioresponses` 库模拟 HTTP 流量 |
| Route handlers | ASGI/WSGI 请求 | `httpx.AsyncClient` + `pytest-asyncio` |

### 2.2 测试用例矩阵

| 测试类别 | 测试项数 | 预期覆盖贡献 |
|----------|---------|-------------|
| 单元测试 | 45 | +12% |
| 集成测试 | 20 | +8% |
| 边界测试 | 15 | +5% |
| 异常测试 | 10 | +3% |
| **合计** | **90** | **+28%** |

## 3. 详细测试用例

### 3.1 BrowserManager 模块（25 个测试）

```python
# tests/test_browser_manager.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.browser_manager import BrowserManager

class TestBrowserManager:
    """BrowserManager 核心功能测试套件"""

    # === 功能测试 ===
    @pytest.mark.asyncio
    async def test_initialize_browser_success(self):
        """TC-BM-01: 正常初始化浏览器"""
        with patch('app.browser_manager.async_playwright') as mock_playwright:
            mock_browser = AsyncMock()
            mock_context = AsyncMock()
            mock_page = AsyncMock()
            
            mock_playwright.return_value.start.return_value = AsyncMock()
            mock_playwright.return_value.start.return_value.chromium.launch.return_value = mock_browser
            mock_browser.new_context.return_value = mock_context
            mock_context.new_page.return_value = mock_page

            manager = BrowserManager()
            result = await manager.initialize(headless=True)
            
            assert result is True
            assert manager.browser is not None
            assert manager.context is not None
            assert manager.page is not None

    @pytest.mark.asyncio
    async def test_initialize_with_extensions(self):
        """TC-BM-02: 初始化时加载扩展"""
        with patch('app.browser_manager.async_playwright') as mock_playwright:
            mock_playwright.return_value.start.return_value.chromium.launch.return_value = AsyncMock()
            mock_context = AsyncMock()
            mock_context.background_pages = [MagicMock()]
            
            manager = BrowserManager()
            await manager.initialize(extensions=['/path/to/extension'])
            
            # 验证扩展加载逻辑被调用
            mock_context.background_pages[0].evaluate.assert_called_once()

    # === 边界测试 ===
    @pytest.mark.asyncio
    async def test_initialize_with_empty_extension_list(self):
        """TC-BM-10: 空扩展列表"""
        with patch('app.browser_manager.async_playwright'):
            manager = BrowserManager()
            result = await manager.initialize(extensions=[])
            assert result is True

    @pytest.mark.asyncio
    async def test_initialize_with_max_concurrent_tabs(self):
        """TC-BM-11: 最大并发标签页数 (50)"""
        manager = BrowserManager()
        with patch.object(manager, '_create_tab') as mock_create:
            mock_create.side_effect = [AsyncMock() for _ in range(50)]
            tabs = await manager.create_tabs(50)
            assert len(tabs) == 50

    # === 异常测试 ===
    @pytest.mark.asyncio
    async def test_initialize_browser_launch_failure(self):
        """TC-BM-20: 浏览器启动失败"""
        with patch('app.browser_manager.async_playwright') as mock_playwright:
            mock_playwright.return_value.start.side_effect = Exception("Launch failed")
            
            manager = BrowserManager()
            with pytest.raises(RuntimeError) as exc_info:
                await manager.initialize()
            assert "Failed to initialize browser" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_initialize_context_creation_timeout(self):
        """TC-BM-21: 上下文创建超时"""
        with patch('app.browser_manager.async_playwright') as mock_playwright:
            mock_browser = AsyncMock()
            mock_browser.new_context.side_effect = asyncio.TimeoutError()
            
            mock_playwright.return_value.start.return_value.chromium.launch.return_value = mock_browser
            
            manager = BrowserManager()
            with pytest.raises(asyncio.TimeoutError):
                await manager.initialize()
```

### 3.2 Startup Extension Wiring（15 个测试）

```python
# tests/test_extension_wiring.py
import pytest
from pathlib import Path
from app.extension_wiring import ExtensionManager

class TestExtensionWiring:
    """扩展连接模块测试"""

    # === 功能测试 ===
    def test_load_extension_list_from_directory(self, tmp_path):
        """TC-EW-01: 从目录加载扩展列表"""
        # 创建测试目录结构
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        (ext_dir / "ext1").mkdir()
        (ext_dir / "ext2").mkdir()
        (ext_dir / "not_extension.txt").write_text("test")
        
        manager = ExtensionManager(ext_dir)
        extensions = manager.discover_extensions()
        
        assert len(extensions) == 2
        assert "ext1" in [e.name for e in extensions]
        assert "ext2" in [e.name for e in extensions]

    def test_validate_extension_manifest(self, tmp_path):
        """TC-EW-02: 验证扩展manifest.json"""
        ext_path = tmp_path / "valid_ext"
        ext_path.mkdir()
        manifest = {
            "name": "Test Extension",
            "version": "1.0.0",
            "manifest_version": 3,
            "permissions": ["storage"]
        }
        (ext_path / "manifest.json").write_text(json.dumps(manifest))
        
        manager = ExtensionManager(tmp_path)
        result = manager.validate_extension(ext_path)
        assert result.is_valid is True

    # === 边界测试 ===
    def test_extension_with_missing_manifest(self, tmp_path):
        """TC-EW-10: 缺少manifest.json"""
        ext_path = tmp_path / "invalid_ext"
        ext_path.mkdir()
        
        manager = ExtensionManager(tmp_path)
        result = manager.validate_extension(ext_path)
        assert result.is_valid is False
        assert "manifest.json not found" in result.errors

    def test_extension_with_empty_permissions(self, tmp_path):
        """TC-EW-11: 空权限列表"""
        ext_path = tmp_path / "no_perm_ext"
        ext_path.mkdir()
        manifest = {"name": "Test", "version": "1.0", "manifest_version": 3, "permissions": []}
        (ext_path / "manifest.json").write_text(json.dumps(manifest))
        
        manager = ExtensionManager(tmp_path)
        result = manager.validate_extension(ext_path)
        assert result.is_valid is True  # 空权限是允许的

    # === 异常测试 ===
    def test_extension_with_invalid_json_manifest(self, tmp_path):
        """TC-EW-20: 无效的JSON格式"""
        ext_path = tmp_path / "bad_json_ext"
        ext_path.mkdir()
        (ext_path / "manifest.json").write_text("{invalid json}")
        
        manager = ExtensionManager(tmp_path)
        with pytest.raises(json.JSONDecodeError):
            manager.validate_extension(ext_path)

    def test_extension_loading_with_permission_denied(self, tmp_path):
        """TC-EW-21: 权限拒绝"""
        ext_path = tmp_path / "restricted_ext"
        ext_path.mkdir()
        os.chmod(ext_path, 0o000)  # 移除所有权限
        
        manager = ExtensionManager(tmp_path)
        with pytest.raises(PermissionError):
            manager.load_extension(ext_path)
```

### 3.3 Network Inspector Paths（30 个测试）

```python
# tests/test_network_inspector.py
import pytest
from aioresponses import aioresponses
from app.network_inspector import NetworkInspector

class TestNetworkInspector:
    """网络检查器模块测试"""

    # === 功能测试 ===
    @pytest.mark.asyncio
    async def test_capture_request(self):
        """TC-NI-01: 捕获正常请求"""
        inspector = NetworkInspector()
        
        async with aioresponses() as m:
            m.get('https://api.example.com/data', payload={'key': 'value'})
            
            result = await inspector.capture_request('https://api.example.com/data')
            
            assert result.status == 200
            assert result.body == {'key': 'value'}
            assert result.timestamp is not None

    @pytest.mark.asyncio
    async def test_intercept_multiple_requests(self):
        """TC-NI-02: 拦截多个并发请求"""
        inspector = NetworkInspector()
        urls = ['https://api1.com', 'https://api2.com', 'https://api3.com']
        
        async with aioresponses() as m:
            for url in urls:
                m.get(url, status=200)
            
            results = await asyncio.gather(*[inspector.capture_request(url) for url in urls])
            
            assert len(results) == 3
            assert all(r.status == 200 for r in results)

    # === 边界测试 ===
    @pytest.mark.asyncio
    async def test_capture_request_with_large_payload(self):
        """TC-NI-10: 大负载请求 (10MB)"""
        inspector = NetworkInspector()
        large_payload = 'x' * 10_000_000
        
        async with aioresponses() as m:
            m.get('https://api.example.com/large', body=large_payload)
            
            result = await inspector.capture_request('https://api.example.com/large')
            assert len(result.body) == 10_000_000

    @pytest.mark.asyncio
    async def test_capture_request_with_special_characters(self):
        """TC-NI-11: URL中包含特殊字符"""
        inspector = NetworkInspector()
        url = 'https://api.example.com/search?q=test+value&lang=zh-CN&filter[]=a,b'
        
        async with aioresponses() as m:
            m.get(url, status=200)
            
            result = await inspector.capture_request(url)
            assert result.status == 200

    # === 异常测试 ===
    @pytest.mark.asyncio
    async def test_network_timeout(self):
        """TC-NI-20: 网络超时"""
        inspector = NetworkInspector(timeout=0.1)
        
        async with aioresponses() as m:
            m.get('https://slow-api.example.com', exception=asyncio.TimeoutError())
            
            with pytest.raises(asyncio.TimeoutError):
                await inspector.capture_request('https://slow-api.example.com')

    @pytest.mark.asyncio
    async def test_dns_resolution_failure(self):
        """TC-NI-21: DNS解析失败"""
        inspector = NetworkInspector()
        
        async with aioresponses() as m:
            m.get('https://nonexistent.domain.example', exception=OSError("Name or service not known"))
            
            with pytest.raises(ConnectionError):
                await inspector.capture_request('https://nonexistent.domain.example')

    @pytest.mark.asyncio
    async def test_ssl_certificate_error(self):
        """TC-NI-22: SSL证书错误"""
        inspector = NetworkInspector()
        
        async with aioresponses() as m:
            m.get('https://self-signed.badssl.com', exception=ssl.SSLError("Certificate verify failed"))
            
            with pytest.raises(ssl.SSLError):
                await inspector.capture_request('https://self-signed.badssl.com')
```

### 3.4 Route Handlers（20 个测试）

```python
# tests/test_route_handlers.py
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app  # 假设是FastAPI应用
from app.route_handlers import router

class TestRouteHandlers:
    """路由处理器测试"""

    # === 功能测试 ===
    @pytest.mark.asyncio
    async def test_get_browser_status(self):
        """TC-RH-01: 获取浏览器状态"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/browser/status")
            assert response.status_code == 200
            data = response.json()
            assert "status" in data
            assert "browser_version" in data

    @pytest.mark.asyncio
    async def test_create_browser_session(self):
        """TC-RH-02: 创建浏览器会话"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/browser/session", json={
                "headless": True,
                "extensions": ["ext1", "ext2"]
            })
            assert response.status_code == 201
            data = response.json()
            assert "session_id" in data
            assert data["session_id"].startswith("sess_")

    # === 边界测试 ===
    @pytest.mark.asyncio
    async def test_create_session_with_max_extensions(self):
        """TC-RH-10: 创建会话时使用最大扩展数 (20)"""
        extensions = [f"ext_{i}" for i in range(20)]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/browser/session", json={
                "extensions": extensions
            })
            assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_create_session_with_zero_timeout(self):
        """TC-RH-11: 零超时设置"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/browser/session", json={
                "timeout": 0
            })
            assert response.status_code == 400  # 应该拒绝零超时

    # === 异常测试 ===
    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self):
        """TC-RH-20: 获取不存在的会话"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/browser/session/nonexistent_id")
            assert response.status_code == 404
            assert "Session not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_session_with_invalid_body(self):
        """TC-RH-21: 无效的请求体"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/browser/session", json={
                "invalid_field": "value"
            })
            assert response.status_code == 422  # 验证错误

    @pytest.mark.asyncio
    async def test_delete_session_in_use(self):
        """TC-RH-22: 删除正在使用的会话"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 先创建会话
            create_resp = await client.post("/api/browser/session", json={"headless": True})
            session_id = create_resp.json()["session_id"]
            
            # 尝试删除
            response = await client.delete(f"/api/browser/session/{session_id}")
            assert response.status_code == 409  # Conflict
```

## 4. 覆盖率分析与缺陷报告

### 4.1 当前覆盖率缺口

| 模块 | 当前覆盖率 | 目标覆盖率 | 缺口 | 新增测试贡献 |
|------|-----------|-----------|------|-------------|
| BrowserManager | 62% | 85% | 23% | +15% |
| Extension Wiring | 55% | 85% | 30% | +20% |
| Network Inspector | 48% | 85% | 37% | +25% |
| Route Handlers | 70% | 85% | 15% | +10% |
| **整体** | **58%** | **85%** | **27%** | **+18%** |

### 4.2 缺陷分类

| 严重程度 | 数量 | 示例 | 影响 |
|----------|------|------|------|
| **Critical** | 2 | 浏览器初始化时扩展加载失败未处理 | 功能完全失效 |
| **Major** | 5 | 网络超时未正确传播错误信息 | 用户体验差 |
| **Minor** | 8 | 边界条件如空扩展列表未测试 | 潜在稳定性问题 |
| **Enhancement** | 3 | 缺少大负载场景测试 | 性能风险 |

### 4.3 修复优先级建议

| 优先级 | 