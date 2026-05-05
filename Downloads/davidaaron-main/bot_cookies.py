# bot_cookies.py v3.2 - Proxy completo SUNAT
# ✅ Login con Playwright (headless)
# ✅ Proxy completo: HTML + JS + CSS + fuentes + AJAX
# ✅ Elimina X-Frame-Options (para funcionar en iframe)
# ✅ Reescribe todas las URLs para pasar por el proxy
# ✅ Intercepta AJAX de SUNAT y los redirige correctamente

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from starlette.responses import JSONResponse
from playwright.async_api import async_playwright
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin, quote, unquote
import logging
import os
import uuid
import re
import json
import httpx
import asyncio
import random

# ===============================
# CONFIGURACIÓN
# ===============================

API_KEY = os.environ.get("COOKIES_API_KEY")
if not API_KEY:
    raise RuntimeError("COOKIES_API_KEY environment variable is required")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot_cookies")

PW_NAV_TIMEOUT    = int(os.environ.get("COOKIES_NAV_TIMEOUT_MS", "30000"))
PROXY_TTL_MIN     = int(os.environ.get("COOKIES_PROXY_TTL_MIN", "120"))  # 2 horas por defecto
RATE_LIMIT_WINDOW = int(os.environ.get("COOKIES_RATE_LIMIT_WINDOW", "60"))
RATE_LIMIT_MAX    = int(os.environ.get("COOKIES_RATE_LIMIT_MAX", "20"))

# URL base del proxy (ajustar si se despliega en servidor)
PROXY_BASE = os.environ.get("COOKIES_PROXY_BASE", "http://localhost:8001")

# Archivo para persistir sesiones entre reinicios
SESSIONS_FILE = os.environ.get("COOKIES_SESSIONS_FILE", "proxy_sessions.json")

request_times: dict[str, list[datetime]] = {}

# ===============================
# PLAYWRIGHT GLOBAL (REUSO)
# ===============================
# Se inicializa una sola vez en startup; el navegador NO se cierra entre requests.
# Cada request crea un nuevo context+page (stateless) y luego se cierran.
playwright_instance = None
browser = None


def _load_sessions_from_disk() -> dict:
    """Carga sesiones guardadas en disco. Descarta las expiradas."""
    if not os.path.exists(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = datetime.now()
        sessions = {}
        for token, s in data.items():
            try:
                s["expires_at"] = datetime.fromisoformat(s["expires_at"])
                if s["expires_at"] > now:
                    sessions[token] = s
            except Exception:
                pass
        logger.info(f"[Sessions] Cargadas {len(sessions)} sesiones desde disco")
        return sessions
    except Exception as e:
        logger.warning(f"[Sessions] No se pudo cargar {SESSIONS_FILE}: {e}")
        return {}


def _save_sessions_to_disk():
    """Persiste sesiones activas a disco."""
    try:
        data = {}
        now = datetime.now()
        for token, s in proxy_sessions.items():
            if s["expires_at"] > now:
                sc = dict(s)
                sc["expires_at"] = s["expires_at"].isoformat()
                data[token] = sc
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[Sessions] No se pudo guardar sesiones: {e}")


proxy_sessions: dict[str, dict] = _load_sessions_from_disk()

# ===============================
# URLs SUNAT
# ===============================

SUNAT_URL       = "https://www.sunat.gob.pe/"
SUNAT_LOGIN_URL = "https://e-menu.sunat.gob.pe/cl-ti-itmenu/MenuInternet.htm"
SUNAT_MENU_URL  = "https://e-menu.sunat.gob.pe/cl-ti-itmenu/MenuInternet.htm?pestana=*&agrupacion=*"
SUNAT_ORIGIN    = "https://e-menu.sunat.gob.pe"

SUNAT_DECLARACION_LOGIN_URL = (
    "https://api-seguridad.sunat.gob.pe/v1/clientessol/59d39217-c025-4de5-b342-393b0f4630ab/"
    "oauth2/loginMenuSol?lang=es-PE&showDni=true&showLanguages=false&"
    "originalUrl=https://e-menu.sunat.gob.pe/cl-ti-itmenu2/AutenticaMenuInternetPlataforma.htm&"
    "state=rO0ABXQA701GcmNEbDZPZ28xODJOWWQ4aTNPT2srWUcrM0pTODAzTEJHTmtLRE1IT2pBQ2l2eW84em5l"
    "WjByM3RGY1BLT0tyQjEvdTBRaHNNUW8KWDJRQ0h3WmZJQWZyV0JBaGtTT0hWajVMZEg0Mm5ZdHlrQlFVaDFw"
    "MzF1eVl1V2tLS3ozUnVoZ1ovZisrQkZndGdSVzg1TXdRTmRhbAp1ek5OaXdFbG80TkNSK0E2NjZHeG0zNkNa"
    "M0NZL0RXa1FZOGNJOWZsYjB5ZXc3MVNaTUpxWURmNGF3dVlDK3pMUHdveHI2cnNIaWc1CkI3SkxDSnc9"
)
# URL destino final del portal Declaración y Pago (después del login)
SUNAT_DECLARACION_REDIRECT_URL = "https://e-menu.sunat.gob.pe/cl-ti-itmenu2/MenuInternetPlataforma.htm?pestana=*&agrupacion=*&exe=55.1.1.1.1"

SUNAFIL_CASILLA_ORIGINAL_URL = "https://casillaelectronica.sunafil.gob.pe/si.inbox/Login/Empresa"
SUNAFIL_CASILLA_LOGIN_URL = (
    "https://api-seguridad.sunat.gob.pe/v1/clientessol/b6474e23-8a3b-4153-b301-dafcc9646250/"
    "oauth2/login?originalUrl=" + SUNAFIL_CASILLA_ORIGINAL_URL + "&state=s"
)

# ===============================
# FASTAPI
# ===============================

app = FastAPI(title="Bot Cookies SUNAT + SUNAFIL v3", version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    global playwright_instance, browser
    if playwright_instance and browser:
        return

    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--disable-infobars",
            "--disable-extensions",
        ],
    )
    logger.info("[Startup] Playwright + Chromium iniciado ✅")


@app.on_event("shutdown")
async def shutdown():
    global playwright_instance, browser
    try:
        if browser:
            await browser.close()
            logger.info("[Shutdown] Chromium cerrado ✅")
    finally:
        browser = None
        if playwright_instance:
            await playwright_instance.stop()
            logger.info("[Shutdown] Playwright detenido ✅")
        playwright_instance = None

# ===============================
# MODELOS
# ===============================

class Credenciales(BaseModel):
    ruc: str
    usuario_sol: str
    clave_sol: str
    portal: str = "sunat"

# ===============================
# MIDDLEWARES
# ===============================

def get_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    public = ["/health", "/", "/docs", "/openapi.json"]
    public_prefixes = ("/proxy/", "/get-cookies/", "/ext-inject/", "/session-redirect/", "/buzon/")
    if request.url.path.startswith(public_prefixes) or request.url.path in public:
        return await call_next(request)
    if request.headers.get("x-api-key") != API_KEY:
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid_api_key"})
    return await call_next(request)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    public = ["/health", "/", "/docs", "/openapi.json"]
    public_prefixes = ("/proxy/", "/get-cookies/", "/ext-inject/", "/session-redirect/", "/buzon/")
    if request.url.path.startswith(public_prefixes) or request.url.path in public:
        return await call_next(request)
    ip = get_client_ip(request)
    now = datetime.now()
    times = [t for t in request_times.get(ip, []) if now - t <= timedelta(seconds=RATE_LIMIT_WINDOW)]
    times.append(now)
    request_times[ip] = times
    if len(times) > RATE_LIMIT_MAX:
        return JSONResponse(status_code=429, content={"ok": False, "error": "rate_limited"})
    return await call_next(request)

# ===============================
# HELPERS PROXY
# ===============================

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _cleanup_proxy_sessions():
    now = datetime.now()
    expired = [t for t, v in proxy_sessions.items() if v["expires_at"] <= now]
    for t in expired:
        del proxy_sessions[t]
    if expired:
        logger.info(f"[GC] {len(expired)} proxy sessions expiradas")


def _cookies_to_header(cookies: list) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _filter_cookies(cookies: list, domains: list) -> list:
    result = []
    for c in cookies:
        domain = c.get("domain", "")
        if any(d in domain for d in domains):
            result.append({
                "name":     c.get("name"),
                "value":    c.get("value"),
                "domain":   domain,
                "path":     c.get("path", "/"),
                "secure":   c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": c.get("sameSite", "Lax"),
            })
    return result


# Extensiones de activos estáticos que no necesitan pasar por el proxy
# ⚠️ Fuentes (.woff, .woff2, .ttf, .eot, .otf) NO están aquí — deben pasar
#    por el proxy para recibir Access-Control-Allow-Origin: * (CORS)
STATIC_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".mp4", ".webm", ".mp3", ".wav",
}


def _make_proxy_url(token: str, target_url: str) -> str:
    """Genera URL del proxy para un recurso de SUNAT."""
    encoded = quote(target_url, safe="")
    return f"{PROXY_BASE}/proxy/{token}/r?url={encoded}"


def _rewrite_html(html: str, token: str, base_url: str) -> str:
    """
    Reescribe el HTML para que todos los recursos pasen por el proxy.
    Maneja: src=, href=, action=, url(), @import, fetch(), XMLHttpRequest
    """

    def make_absolute(url: str) -> str:
        """Convierte URL relativa a absoluta."""
        if not url or url.startswith("data:") or url.startswith("javascript:") or url.startswith("#"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return urljoin(base_url, url)

    def to_proxy(url: str) -> str:
        abs_url = make_absolute(url)
        if not abs_url or abs_url.startswith("data:") or abs_url.startswith("javascript:"):
            return url
        # Activos estáticos: carga directa desde SUNAT (evita saturar ngrok)
        try:
            ext = os.path.splitext(urlparse(abs_url).path)[1].lower()
        except Exception:
            ext = ""
        if ext in STATIC_EXTENSIONS:
            return abs_url
        # Solo proxear dominios de SUNAT
        if any(d in abs_url for d in ["sunat.gob.pe", "sunafil.gob.pe"]):
            return _make_proxy_url(token, abs_url)
        return abs_url

    # --- Reescribir src= ---
    def rewrite_src(m):
        quote_char = m.group(1)
        url = m.group(2)
        return f'src={quote_char}{to_proxy(url)}{quote_char}'

    html = re.sub(r'src=(["\'])([^"\']+)\1', rewrite_src, html)

    # --- Reescribir href= (excepto anchors y mailto) ---
    def rewrite_href(m):
        quote_char = m.group(1)
        url = m.group(2)
        if url.startswith("#") or url.startswith("mailto:"):
            return m.group(0)
        return f'href={quote_char}{to_proxy(url)}{quote_char}'

    html = re.sub(r'href=(["\'])([^"\']+)\1', rewrite_href, html)

    # --- Reescribir action= (formularios) ---
    def rewrite_action(m):
        quote_char = m.group(1)
        url = m.group(2)
        return f'action={quote_char}{to_proxy(url)}{quote_char}'

    html = re.sub(r'action=(["\'])([^"\']+)\1', rewrite_action, html)

    # --- Reescribir url() en CSS inline ---
    def rewrite_css_url(m):
        url = m.group(1).strip("'\"")
        return f'url({to_proxy(url)})'

    html = re.sub(r'url\(([^)]+)\)', rewrite_css_url, html)

    # --- Inyectar script interceptor de AJAX/fetch al inicio del <head> ---
    interceptor = f"""
<script>
(function() {{
    var TOKEN = '{token}';
    var PROXY_BASE = '{PROXY_BASE}';
    var BASE_URL = '{base_url}';
    var SUNAT_DOMAINS = ['sunat.gob.pe', 'sunafil.gob.pe'];

    // jQuery stub — previene "$ is not defined" en subcódigo SUNAT (gettime.pl, etc.)
    // SUNAT carga gettime.pl en un iframe que asume parent.$ disponible;
    // como rompemos la cadena parent, inyectamos un stub si $ no está cargado aún.
    if (typeof window.$ === 'undefined') {{
        window.$ = window.jQuery = function(sel) {{
            var els = [];
            try {{ if (typeof sel === 'string') els = Array.prototype.slice.call(document.querySelectorAll(sel)); }} catch(e) {{}}
            var jq = {{
                ready:       function(f) {{ if (document.readyState !== 'loading') {{ setTimeout(f, 0); }} else {{ document.addEventListener('DOMContentLoaded', f); }} return jq; }},
                find:        function(s) {{ return window.$(s); }},
                html:        function(h) {{ if (h !== undefined) {{ els.forEach(function(e) {{ e.innerHTML = h; }}); return jq; }} return els[0] ? els[0].innerHTML : ''; }},
                val:         function(v) {{ if (v !== undefined) {{ els.forEach(function(e) {{ e.value = v; }}); return jq; }} return els[0] ? (els[0].value || '') : ''; }},
                text:        function(t) {{ if (t !== undefined) {{ els.forEach(function(e) {{ e.textContent = t; }}); return jq; }} return els[0] ? (els[0].textContent || '') : ''; }},
                show:        function() {{ els.forEach(function(e) {{ e.style.display = ''; }}); return jq; }},
                hide:        function() {{ els.forEach(function(e) {{ e.style.display = 'none'; }}); return jq; }},
                on:          function() {{ return jq; }},
                off:         function() {{ return jq; }},
                click:       function(f) {{ if (typeof f === 'function') els.forEach(function(e) {{ e.addEventListener('click', f); }}); return jq; }},
                attr:        function(a, v) {{ if (v !== undefined) {{ els.forEach(function(e) {{ e.setAttribute(a, v); }}); return jq; }} return els[0] ? (els[0].getAttribute(a) || '') : ''; }},
                css:         function() {{ return jq; }},
                addClass:    function() {{ return jq; }},
                removeClass: function() {{ return jq; }},
                prop:        function() {{ return jq; }},
                each:        function(f) {{ els.forEach(function(e, i) {{ f.call(e, i, e); }}); return jq; }},
                length: els.length
            }};
            return jq;
        }};
        window.$.ajax = function(opts) {{
            opts = opts || {{}};
            var d = {{ done: function(f) {{ if (typeof f==='function') setTimeout(function() {{ f({{}}); }}, 10); return d; }}, fail: function() {{ return d; }} }};
            if (typeof opts.success === 'function') setTimeout(function() {{ opts.success({{}}); }}, 10);
            return d;
        }};
        window.$.get = window.$.post = function(url, data, cb) {{
            if (typeof data === 'function') {{ cb = data; }}
            if (typeof cb === 'function') setTimeout(function() {{ cb({{}}); }}, 10);
            return {{ done: function(f) {{ if (typeof f==='function') setTimeout(function() {{ f({{}}); }}, 10); return this; }}, fail: function() {{ return this; }} }};
        }};
        window.$.extend = function() {{
            var deep = false, r = {{}}, i = 0;
            if (typeof arguments[0] === 'boolean') {{ deep = arguments[0]; i = 1; }}
            r = arguments[i] || {{}};
            for (var j = i + 1; j < arguments.length; j++) {{
                var s = arguments[j];
                if (s) for (var k in s) {{ if (Object.prototype.hasOwnProperty.call(s, k)) r[k] = s[k]; }}
            }}
            return r;
        }};
        window.$.noop = function() {{}};
        window.$.fn = {{}};
        window.$.Deferred = function() {{
            var cbs = [], ecbs = [];
            var d = {{
                done:    function(f) {{ if (typeof f==='function') {{ cbs.push(f); }} return d; }},
                fail:    function(f) {{ if (typeof f==='function') {{ ecbs.push(f); }} return d; }},
                always:  function(f) {{ return d.done(f).fail(f); }},
                then:    function(f) {{ return d.done(f); }},
                resolve: function() {{ var a = arguments; cbs.forEach(function(f) {{ try {{ f.apply(null, a); }} catch(e) {{}} }}); return d; }},
                reject:  function() {{ var a = arguments; ecbs.forEach(function(f) {{ try {{ f.apply(null, a); }} catch(e) {{}} }}); return d; }},
                promise: function() {{ return d; }}
            }};
            return d;
        }};
        // ── Métodos estáticos esenciales ──
        window.$.trim        = function(s) {{ return s == null ? '' : String(s).trim(); }};
        window.$.isFunction  = function(x) {{ return typeof x === 'function'; }};
        window.$.isArray     = Array.isArray;
        window.$.isWindow    = function(x) {{ return x != null && x === x.window; }};
        window.$.isNumeric   = function(x) {{ return !isNaN(parseFloat(x)) && isFinite(x); }};
        window.$.isEmptyObject = function(x) {{ for (var k in x) {{ if (Object.prototype.hasOwnProperty.call(x,k)) return false; }} return true; }};
        window.$.isPlainObject = function(x) {{ return x !== null && typeof x === 'object' && Object.getPrototypeOf(x) === Object.prototype; }};
        window.$.type = function(x) {{
            if (x == null) return x + '';
            return Object.prototype.toString.call(x).slice(8,-1).toLowerCase();
        }};
        window.$.each = function(x, f) {{
            if (Array.isArray(x)) {{ for (var i=0; i<x.length; i++) {{ if (f.call(x[i], i, x[i]) === false) break; }} }}
            else {{ for (var k in x) {{ if (Object.prototype.hasOwnProperty.call(x,k)) {{ if (f.call(x[k], k, x[k]) === false) break; }} }} }}
            return x;
        }};
        window.$.map = function(x, f) {{
            var r = [];
            if (Array.isArray(x)) {{ x.forEach(function(v,i) {{ var m=f(v,i); if (m!=null) r=r.concat(m); }}); }}
            else {{ for (var k in x) {{ if (Object.prototype.hasOwnProperty.call(x,k)) {{ var m=f(x[k],k); if (m!=null) r=r.concat(m); }} }} }}
            return r;
        }};
        window.$.grep   = function(x, f, inv) {{ return x.filter(function(v,i) {{ return !!f(v,i) !== !!inv; }}); }};
        window.$.inArray = function(v, arr) {{ return arr ? Array.prototype.indexOf.call(arr, v) : -1; }};
        window.$.merge  = function(a, b) {{ for (var i=0; i<b.length; i++) a.push(b[i]); return a; }};
        window.$.makeArray = function(x) {{ return x == null ? [] : Array.isArray(x) ? x : Array.prototype.slice.call(x); }};
        window.$.proxy  = function(fn, ctx) {{ return function() {{ return fn.apply(ctx, arguments); }}; }};
        window.$.now    = Date.now ? Date.now.bind(Date) : function() {{ return new Date().getTime(); }};
        window.$.parseJSON = function(s) {{ return JSON.parse(s); }};
        window.$.parseHTML = function(html) {{ var d=document.createElement('div'); d.innerHTML=html||''; return Array.prototype.slice.call(d.childNodes); }};
        window.$.error  = function(msg) {{ throw new Error(msg); }};
        window.$.Event  = function(type, props) {{ var e={{type:type,preventDefault:function(){{this.defaultPrevented=true;}},stopPropagation:function(){{}},isDefaultPrevented:function(){{return !!this.defaultPrevented;}}}};  if (props) for (var k in props) e[k]=props[k]; return e; }};
        window.$.contains = function(a, b) {{ return a !== b && a.contains(b); }};
        window.$.globalEval = function(code) {{ var s=document.createElement('script'); s.text=code; document.head.appendChild(s).parentNode.removeChild(s); }};
        window.$.expr = {{ ':': {{}} }};
        window.$.support = {{}};
    }}

    // Verifica solo el hostname real — evita falsos positivos en query params (?url=https://sunat...)
    function isSunatUrl(url) {{
        try {{
            var hostname = new URL(url).hostname;
            return SUNAT_DOMAINS.some(function(d) {{ return hostname.includes(d); }});
        }} catch(e) {{
            return false;
        }}
    }}

    // Retorna null para BLOQUEAR (no navegar), URL proxeada si es SUNAT, o la original.
    // fromIframeSrc=true → la URL viene de un setter de iframe.src (no bloquear menuinternet)
    function toProxyUrl(url, fromIframeSrc) {{
        if (!url || url.startsWith('data:') || url.startsWith('javascript:')) return url;
        if (url.startsWith('//')) url = 'https:' + url;
        if (!url.startsWith('http')) {{
            try {{ url = new URL(url, BASE_URL).href; }} catch(e) {{ return url; }}
        }}
        var urlLower = url.toLowerCase();
        // BLOQUEAR saliendo — case-insensitive, en toda la URL (incluye query params)
        var BLOCK_TERMS = ['saliendo', 'cerrar_ventana', 'cerrarventana', 'mensajesalida', 'salidamenu', 'salida.htm'];
        if (BLOCK_TERMS.some(function(t) {{ return urlLower.includes(t); }})) return null;
        // BLOQUEAR recarga de menuinternet desde location.href (no desde iframe.src)
        if (!fromIframeSrc && (urlLower.includes('/cl-ti-itmenu/menuinternet') || urlLower.includes('/cl-ti-itmenu2/menuinternet'))) return null;
        // Si ya está proxeada verificar que no lleve saliendo en el ?url=
        if (url.startsWith(PROXY_BASE + '/proxy/')) return url;
        if (isSunatUrl(url)) {{
            return PROXY_BASE + '/proxy/' + TOKEN + '/r?url=' + encodeURIComponent(url);
        }}
        return url;
    }}

    // Parcha código dinámico (eval/Function/setTimeout con string)
    function patchCode(code) {{
        if (typeof code !== 'string') return code;
        return code
            .replace(/\\btop\\.location\\b/g, 'window.location')
            .replace(/\\bparent\\.location\\b/g, 'window.location')
            .replace(/\\bself\\s*!={{1,2}}\\s*top\\b/g, 'false')
            .replace(/\\btop\\s*!={{1,2}}\\s*self\\b/g, 'false')
            .replace(/\\bwindow\\s*!={{1,2}}\\s*window\\.top\\b/g, 'false')
            .replace(/\\bwindow\\.top\\s*!={{1,2}}\\s*window\\b/g, 'false')
            .replace(/\\bwindow\\s*!={{1,2}}\\s*top\\b/g, 'false')
            .replace(/\\btop\\s*!={{1,2}}\\s*window\\b/g, 'false')
            .replace(/\\bself\\s*!={{1,2}}\\s*window\\b/g, 'false')
            .replace(/\\bwindow\\s*!={{1,2}}\\s*self\\b/g, 'false')
            .replace(/\\bself\\s*=={{1,3}}\\s*top\\b/g, 'true')
            .replace(/\\btop\\s*=={{1,3}}\\s*self\\b/g, 'true')
            .replace(/\\bwindow\\s*=={{1,3}}\\s*top\\b/g, 'true')
            .replace(/\\btop\\s*=={{1,3}}\\s*window\\b/g, 'true');
    }}

    // Helper: envuelve un objeto Window (frame hijo del frameset) para que
    // toda asignación a .location.href / .location pase por toProxyUrl().
    function _wrapFrameWindow(fw) {{
        if (!fw || typeof fw !== 'object') return fw;
        return new Proxy(fw, {{
            get: function(t, prop) {{
                if (prop === 'location') {{
                    // Devolver un proxy de location que intercepta href setter
                    var _loc = t.location;
                    return new Proxy(_loc, {{
                        get: function(lt, lp) {{
                            var v = lt[lp];
                            if (lp === 'href' && typeof v === 'string') return v;
                            if (typeof v === 'function') return v.bind(lt);
                            return v;
                        }},
                        set: function(lt, lp, val) {{
                            if (lp === 'href') {{
                                console.log('[sunat-proxy] frame.location.href =', val);
                                var n = toProxyUrl(val);
                                console.log('[sunat-proxy] → proxied:', n);
                                if (n === null) return true;
                                lt.href = n;
                                return true;
                            }}
                            lt[lp] = val;
                            return true;
                        }}
                    }});
                }}
                try {{
                    var v2 = t[prop];
                    // Si es otro frame (Window), envolverlo también
                    if (v2 && typeof v2 === 'object' && v2.self === v2) return _wrapFrameWindow(v2);
                    if (typeof v2 === 'function') return v2.bind(t);
                    return v2;
                }} catch(ee) {{ return undefined; }}
            }},
            set: function(t, prop, val) {{
                if (prop === 'location') {{
                    var n = toProxyUrl(val);
                    if (n === null) return true;
                    t.location = n;
                    return true;
                }}
                try {{ t[prop] = val; }} catch(ee) {{}}
                return true;
            }}
        }});
    }}

    // 1. window.top y window.parent
    // REGLA FIJA: top = window SIEMPRE para pasar los checks anti-iframe de SUNAT
    //   (self !== top → false, window !== top → false, etc.)
    // parent: si somos un sub-frame same-origin del frameset SUNAT → _wrapFrameWindow
    //         si somos cross-origin (iframe de Laravel) → window
    //
    // Para que "top.frmContenido" también funcione (SUNAT a veces usa top en vez de parent):
    //   Definimos window.frmContenido / frmMenu / frmHeader como getters perezosos
    //   que devuelven _wrappedPar[name]. Así top.frmContenido = window.frmContenido
    //   = getter → _wrappedPar['frmContenido'] → wrapped frame.
    try {{
        var _realPar = Object.getOwnPropertyDescriptor(Window.prototype, 'parent').get.call(window);
        // top → siempre window (nunca el frame real) para que self === top sea true
        try {{ Object.defineProperty(window, 'top', {{ get: function() {{ return window; }}, configurable: true }}); }} catch(e) {{}}

        if (_realPar !== window) {{
            // Somos un sub-frame (frmMenu, frmContenido, etc.)
            var _wrappedPar;
            try {{
                void _realPar.document; // SecurityError si cross-origin
                _wrappedPar = _wrapFrameWindow(_realPar); // same-origin: frameset de SUNAT
            }} catch(e) {{
                _wrappedPar = window; // cross-origin: iframe de Laravel → ocultar
            }}
            try {{ Object.defineProperty(window, 'parent', {{ get: function() {{ return _wrappedPar; }}, configurable: true }}); }} catch(e) {{}}

            // Exponer frames del padre en window para que top.frmContenido funcione
            // (getters perezosos → evalúan _wrappedPar[name] en el momento de acceso)
            if (_wrappedPar !== window) {{
                var _KNOWN_FRAMES = ['frmContenido','frmMenu','frmHeader','contenido','menu',
                                     'frmCuerpo','frmContenidoPpal','frmPrincipal','frmMain'];
                _KNOWN_FRAMES.forEach(function(fname) {{
                    try {{
                        Object.defineProperty(window, fname, {{
                            get: function() {{ try {{ return _wrappedPar[fname]; }} catch(e) {{ return undefined; }} }},
                            configurable: true
                        }});
                    }} catch(e) {{}}
                }});
                // También exponer frames por índice/nombre desde _realPar.frames
                try {{
                    var _pframes = _realPar.frames;
                    for (var _fi = 0; _fi < _pframes.length; _fi++) {{
                        try {{
                            var _fr = _pframes[_fi];
                            var _fn = '';
                            try {{ _fn = _fr.name || (_fr.frameElement ? _fr.frameElement.name : '') || ''; }} catch(e) {{}}
                            if (_fn && _fn.length > 0) {{
                                (function(n, idx) {{
                                    try {{
                                        Object.defineProperty(window, n, {{
                                            get: function() {{ try {{ return _wrapFrameWindow(_realPar.frames[idx]); }} catch(e) {{ return undefined; }} }},
                                            configurable: true
                                        }});
                                    }} catch(e) {{}}
                                }})(_fn, _fi);
                            }}
                        }} catch(e) {{}}
                    }}
                }} catch(e) {{}}
            }}
        }}
    }} catch(e) {{}}

    // 1b. Interceptar window.frmContenido / frmMenu / etc. SIEMPRE — via DOM.
    // PROBLEMA RAÍZ: En MenuInternet.htm (frameset raíz), su parent es el iframe
    // de Laravel (cross-origin) → el código de arriba solo pone parent=window y
    // no registra los getters de frame. Resultado: window.frmContenido es el frame
    // nativo SIN proxy → SUNAT navega directo a ww1.sunat.gob.pe → X-Frame-Options
    // bloquea → panel en blanco.
    // SOLUCIÓN: Siempre definir getters que usan document.querySelector para
    // obtener el frame via DOM (evita recursión con nuestro propio getter) y lo
    // envuelve en _wrapFrameWindow para interceptar .location.href = url.
    (function() {{
        var _FK = ['frmContenido','frmMenu','frmHeader','contenido','menu',
                   'frmCuerpo','frmContenidoPpal','frmPrincipal','frmMain'];
        function _nativeFrameByName(n) {{
            try {{
                var _el = document.querySelector(
                    'frame[name="' + n + '"], iframe[name="' + n + '"]'
                );
                if (_el && _el.contentWindow) return _el.contentWindow;
            }} catch(e) {{}}
            // Fallback: buscar por índice en frames[]
            try {{
                for (var _i = 0; _i < window.frames.length; _i++) {{
                    try {{
                        var _fr = window.frames[_i];
                        var _frn = '';
                        try {{ _frn = _fr.name || ''; }} catch(_e) {{}}
                        if (!_frn) {{ try {{ _frn = _fr.frameElement ? _fr.frameElement.name : ''; }} catch(_e) {{}} }}
                        if (_frn === n) return _fr;
                    }} catch(e) {{}}
                }}
            }} catch(e) {{}}
            return null;
        }}
        _FK.forEach(function(fn) {{
            try {{
                Object.defineProperty(window, fn, {{
                    get: (function(name) {{
                        return function() {{
                            try {{
                                var _f = _nativeFrameByName(name);
                                if (_f && typeof _f === 'object' && _f.self === _f)
                                    return _wrapFrameWindow(_f);
                                return _f;
                            }} catch(e) {{ return undefined; }}
                        }};
                    }})(fn),
                    configurable: true
                }});
            }} catch(e) {{}}
        }});
    }})();

    // 1c. SOLUCIÓN CLAVE: parchar Location.prototype en el realm de cada sub-frame
    // ─────────────────────────────────────────────────────────────────────────────
    // Chrome no permite sobreescribir window.frmContenido con Object.defineProperty
    // porque las propiedades de frames nombrados en framesets son no-configurables.
    // Resultado: SUNAT accede al frame nativo → frmContenido.location.href = url
    // navega directo a ww1.sunat.gob.pe sin pasar por el proxy.
    //
    // SOLUCIÓN: desde el parent (mismo origen), parchar Location.prototype.href
    // setter en el realm de frmContenido. Así CUALQUIER código que haga
    // frmContenido.location.href = url pasa por nuestro toProxyUrl().
    function _patchFrameRealm(fw) {{
        try {{
            if (!fw || !fw.location) return;
            var _proto = fw.location.constructor.prototype;
            // Guardar original (evitar doble-patch)
            var _pd = Object.getOwnPropertyDescriptor(_proto, 'href');
            if (!_pd || !_pd.set) return;
            var _origSet = _pd._sunat_orig_set || _pd.set;
            var _patchedSet = function(url) {{
                console.log('[sunat-proxy][realm] href setter =', url);
                var n = toProxyUrl(url);
                if (n === null) return;
                _origSet.call(this, n);
            }};
            _patchedSet._sunat_orig_set = _origSet;
            Object.defineProperty(_proto, 'href', {{
                get: _pd.get,
                set: _patchedSet,
                configurable: true
            }});
            // También parchar replace() y assign() en ese realm
            try {{
                var _origReplF = _proto.replace;
                _proto.replace = function(url) {{
                    var n = toProxyUrl(url); if (n === null) return;
                    (_origReplF._sunat_orig || _origReplF).call(this, n);
                }};
                _proto.replace._sunat_orig = _origReplF;
            }} catch(e) {{}}
            try {{
                var _origAsgF = _proto.assign;
                _proto.assign = function(url) {{
                    var n = toProxyUrl(url); if (n === null) return;
                    (_origAsgF._sunat_orig || _origAsgF).call(this, n);
                }};
                _proto.assign._sunat_orig = _origAsgF;
            }} catch(e) {{}}
        }} catch(e) {{
            console.log('[sunat-proxy][realm] patch error:', e.message);
        }}
    }}

    // Ejecutar patch en DOMContentLoaded (frames ya existen en el DOM)
    function _patchAllSubFrames() {{
        try {{
            document.querySelectorAll('frame, iframe').forEach(function(el) {{
                try {{ _patchFrameRealm(el.contentWindow); }} catch(e) {{}}
                // Re-parchar tras cada navegación del frame
                el.addEventListener('load', function() {{
                    try {{ _patchFrameRealm(this.contentWindow); }} catch(e) {{}}
                }});
            }});
        }} catch(e) {{}}
    }}
    if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', _patchAllSubFrames);
    }} else {{
        _patchAllSubFrames();
    }}

    // 2. Location.prototype.href setter (este frame)
    try {{
        var _hd = Object.getOwnPropertyDescriptor(Location.prototype, 'href');
        if (_hd && _hd.set) {{
            Object.defineProperty(Location.prototype, 'href', {{
                get: _hd.get,
                set: function(url) {{
                    console.log('[proxy] location.href=', String(url).substring(0,150));
                    var n = toProxyUrl(url);
                    if (n === null) {{ console.log('[proxy] BLOCKED'); return; }}
                    if (n !== url) console.log('[proxy] rewritten→', String(n).substring(0,150));
                    _hd.set.call(this, n);
                }},
                configurable: true
            }});
        }}
    }} catch(e) {{}}

    // 3. Location.prototype.replace
    try {{
        var _origReplace = Location.prototype.replace;
        Location.prototype.replace = function(url) {{
            var n = toProxyUrl(url);
            if (n === null) return;
            _origReplace.call(this, n);
        }};
    }} catch(e) {{}}

    // 4. Location.prototype.assign
    try {{
        var _origAssign = Location.prototype.assign;
        Location.prototype.assign = function(url) {{
            var n = toProxyUrl(url);
            if (n === null) return;
            _origAssign.call(this, n);
        }};
    }} catch(e) {{}}

    // 5. document.location setter
    try {{
        var _dld = Object.getOwnPropertyDescriptor(Document.prototype, 'location');
        if (_dld && _dld.set) {{
            Object.defineProperty(Document.prototype, 'location', {{
                get: _dld.get,
                set: function(url) {{
                    var n = toProxyUrl(url);
                    if (n === null) return;
                    _dld.set.call(this, n);
                }},
                configurable: true
            }});
        }}
    }} catch(e) {{}}

    // 6. Interceptar eval → parchar checks dinámicos de iframe
    try {{
        var _origEval = window.eval;
        window.eval = function(code) {{
            return _origEval.call(this, patchCode(code));
        }};
    }} catch(e) {{}}

    // 7. Interceptar new Function(...)
    try {{
        var _OrigFunc = Function;
        var _NewFunc = function Function() {{
            var args = Array.prototype.slice.call(arguments);
            if (args.length > 0 && typeof args[args.length - 1] === 'string') {{
                args[args.length - 1] = patchCode(args[args.length - 1]);
            }}
            var Tmp = function() {{ return _OrigFunc.apply(this, args); }};
            Tmp.prototype = _OrigFunc.prototype;
            return new Tmp();
        }};
        _NewFunc.prototype = _OrigFunc.prototype;
        window.Function = _NewFunc;
    }} catch(e) {{}}

    // 8. Interceptar setTimeout con string
    try {{
        var _origST = window.setTimeout;
        window.setTimeout = function(fn, delay) {{
            var rest = Array.prototype.slice.call(arguments, 2);
            return _origST.apply(window, [patchCode(fn), delay].concat(rest));
        }};
    }} catch(e) {{}}

    // 9. Interceptar setInterval con string
    try {{
        var _origSI = window.setInterval;
        window.setInterval = function(fn, delay) {{
            var rest = Array.prototype.slice.call(arguments, 2);
            return _origSI.apply(window, [patchCode(fn), delay].concat(rest));
        }};
    }} catch(e) {{}}

    // 10. XMLHttpRequest — reescribir URLs + interceptar isActivo de SUNAT
    var _origXhrOpen = XMLHttpRequest.prototype.open;
    var _origXhrSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url, async, user, password) {{
        // Normalizar URL relativa
        var absUrl = url;
        if (url && !url.startsWith('http') && !url.startsWith('data:') && !url.startsWith('//')) {{
            try {{ absUrl = new URL(url, BASE_URL).href; }} catch(e) {{ absUrl = url; }}
        }}
        var n = toProxyUrl(absUrl);
        this._sunat_blocked = (n === null);
        // Detectar llamadas de sesión/actividad a MenuInternet (isActivo, etc.)
        var urlLower2 = (absUrl || '').toLowerCase();
        // isActivo: SOLO interceptar el keepalive gettime.pl
        this._sunat_isActivo = urlLower2.includes('gettime.pl');
        if (this._sunat_blocked) {{
            // Abrir hacia un endpoint inerte — responderemos en send()
            return _origXhrOpen.call(this, 'GET', 'about:blank', true);
        }}
        return _origXhrOpen.call(this, method, (n !== null ? n : absUrl), async, user, password);
    }};
    XMLHttpRequest.prototype.send = function(body) {{
        if (this._sunat_blocked || this._sunat_isActivo) {{
            // Simular respuesta exitosa de sesión activa
            var self2 = this;
            setTimeout(function() {{
                Object.defineProperty(self2, 'readyState', {{ get: function() {{ return 4; }}, configurable: true }});
                Object.defineProperty(self2, 'status',    {{ get: function() {{ return 200; }}, configurable: true }});
                Object.defineProperty(self2, 'responseText', {{ get: function() {{ return '{{\"activo\":true,\"ok\":true}}'; }}, configurable: true }});
                Object.defineProperty(self2, 'response',     {{ get: function() {{ return '{{\"activo\":true,\"ok\":true}}'; }}, configurable: true }});
                if (typeof self2.onreadystatechange === 'function') self2.onreadystatechange();
                if (typeof self2.onload === 'function') self2.onload({{}}); 
            }}, 10);
            return;
        }}
        return _origXhrSend.apply(this, arguments);
    }};

    // 11. fetch
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {{
        if (typeof url === 'string') {{
            var n = toProxyUrl(url);
            if (n !== null) url = n;
        }}
        return _origFetch.call(this, url, opts);
    }};

    // 12. window.open
    var _origWinOpen = window.open;
    window.open = function(url, name, specs) {{
        try {{
            if (url) {{
                var n = toProxyUrl(url);
                if (n === null) return null;
                url = n;
            }}
        }} catch(e) {{}}
        return _origWinOpen.call(window, url, name, specs);
    }};

    // 13. SUNAT ejecuta()
    window.ejecuta = function(url) {{
        if (!url) return;
        if (url.startsWith('javascript:')) {{ try {{ eval(url.slice(11)); }} catch(e) {{}} return; }}
        var abs = url.startsWith('http') ? url : (function(){{ try {{ return new URL(url, BASE_URL).href; }} catch(e) {{ return url; }} }})();
        var p = toProxyUrl(abs);
        if (p === null) return;
        window.location.href = p;
    }};

    // 14. Click interceptor
    document.addEventListener('click', function(e) {{
        var el = e.target;
        while (el && el.tagName !== 'A') el = el.parentElement;
        if (!el) return;
        var href = el.getAttribute('href');
        if (!href || href === '#' || href.startsWith('javascript:')) return;
        var abs = href.startsWith('http') ? href : (function(){{ try {{ return new URL(href, BASE_URL).href; }} catch(ex) {{ return href; }} }})();
        if (isSunatUrl(abs)) {{
            e.preventDefault();
            e.stopPropagation();
            var p = toProxyUrl(abs);
            if (p === null) return;
            window.location.href = p;
        }}
    }}, true);

    // 15. frameElement
    try {{
        Object.defineProperty(window, 'frameElement', {{ get: function() {{ return null; }}, configurable: true }});
    }} catch(e) {{}}

    // 16. beforeunload
    window.addEventListener('beforeunload', function(e) {{
        e.stopImmediatePropagation();
        return undefined;
    }}, true);

    // 17. document.open — evita que SUNAT limpie el DOM y elimine los interceptores
    try {{
        Document.prototype.open = function() {{ return this; }};
    }} catch(e) {{}}

    // 18. document.write / writeln — bloquear inyección de contenido "saliendo"
    try {{
        var _origDocWrite = Document.prototype.write;
        Document.prototype.write = function(html) {{
            if (typeof html === 'string' &&
                (html.includes('saliendo') || html.includes('cerrar_ventana') || html.includes('cerrarventana'))) {{
                return;
            }}
            return _origDocWrite.apply(this, arguments);
        }};
        Document.prototype.writeln = Document.prototype.write;
    }} catch(e) {{}}

    // 19. HTMLFormElement.submit — bloquear envío de formularios a páginas de salida
    try {{
        var _origSubmit = HTMLFormElement.prototype.submit;
        HTMLFormElement.prototype.submit = function() {{
            var action = (this.action || '').toLowerCase();
            var BLOCK_TERMS2 = ['saliendo', 'cerrar_ventana', 'cerrarventana', 'mensajesalida', 'salidamenu'];
            if (BLOCK_TERMS2.some(function(t) {{ return action.includes(t); }})) return;
            return _origSubmit.apply(this, arguments);
        }};
    }} catch(e) {{}}

    // 20. HTMLIFrameElement.src + HTMLFrameElement.src — interceptar navegación dinámica de frames
    try {{
        ['HTMLIFrameElement', 'HTMLFrameElement'].forEach(function(cls) {{
            if (!window[cls]) return;
            var _fd = Object.getOwnPropertyDescriptor(window[cls].prototype, 'src');
            if (_fd && _fd.set) {{
                Object.defineProperty(window[cls].prototype, 'src', {{
                    get: _fd.get,
                    set: function(url) {{
                        console.log('[proxy] ' + cls + '.src=', String(url).substring(0,150));
                        var n = toProxyUrl(url, true);
                        if (n === null) {{ _fd.set.call(this, 'about:blank'); return; }}
                        if (n !== url) console.log('[proxy] iframe src→', String(n).substring(0,150));
                        _fd.set.call(this, n);
                    }},
                    configurable: true
                }});
            }}
        }});
    }} catch(e) {{}}

    // 21. MutationObserver — capturar cambios de src en iframes inyectados dinámicamente
    try {{
        var _mo = new MutationObserver(function(mutations) {{
            mutations.forEach(function(m) {{
                if (m.type === 'attributes' && m.attributeName === 'src') {{
                    var el = m.target;
                    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {{
                        var src = el.getAttribute('src') || '';
                        if (!src || src === 'about:blank') return;
                        var n = toProxyUrl(src, true);
                        if (n === null) {{ el.setAttribute('src', 'about:blank'); }}
                        else if (n !== src) {{ el.setAttribute('src', n); }}
                    }}
                }}
                if (m.type === 'childList') {{
                    m.addedNodes.forEach(function(node) {{
                        if (node.tagName === 'IFRAME' || node.tagName === 'FRAME') {{
                            console.log('[proxy] frame injected name=', node.name, 'id=', node.id);
                            var src = node.getAttribute('src') || '';
                            if (src && src !== 'about:blank') {{
                                var n = toProxyUrl(src, true);
                                if (n === null) node.setAttribute('src', 'about:blank');
                                else if (n !== src) node.setAttribute('src', n);
                            }}
                            // Parchear realm al cargar para interceptar location.href en ese frame
                            node.addEventListener('load', function() {{
                                console.log('[proxy] dynamic frame loaded name=', this.name);
                                try {{ _patchFrameRealm(this.contentWindow); }} catch(e) {{}}
                            }});
                            try {{ _patchFrameRealm(node.contentWindow); }} catch(e) {{}}
                        }}
                    }});
                }}
            }});
        }});
        _mo.observe(document.documentElement, {{
            subtree: true,
            attributes: true,
            attributeFilter: ['src'],
            childList: true
        }});
    }} catch(e) {{}}

    // Interceptar iframe interno de SUNAT (iframeApplication)
    (function() {{
        var desc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'src');
        if (!desc || !desc.set) return;
        var origSet = desc.set;
        Object.defineProperty(HTMLIFrameElement.prototype, 'src', {{
            set: function(url) {{
                if (url && url !== 'about:blank' && typeof url === 'string' && isSunatUrl(url)) {{
                    console.log('[proxy] iframeApp.src =', String(url).substring(0,150));
                    origSet.call(this, toProxyUrl(url, true));
                }} else {{
                    origSet.call(this, url);
                }}
            }},
            get: desc.get,
            configurable: true
        }});
    }})();

    // Interceptar setAttribute para iframes
    var origSetAttr = Element.prototype.setAttribute;
    Element.prototype.setAttribute = function(name, value) {{
        if (name === 'src' && (this.tagName === 'IFRAME' || this.tagName === 'FRAME') &&
            value && value !== 'about:blank' && typeof value === 'string' && isSunatUrl(value)) {{
            console.log('[proxy] setAttribute src =', String(value).substring(0,150));
            origSetAttr.call(this, name, toProxyUrl(value, true));
        }} else {{
            origSetAttr.call(this, name, value);
        }}
    }};

}})();
</script>
"""

    # Insertar interceptor justo después de <head>
    if "<head>" in html:
        html = html.replace("<head>", "<head>\n" + interceptor, 1)
    elif "<HEAD>" in html:
        html = html.replace("<HEAD>", "<HEAD>\n" + interceptor, 1)
    else:
        html = interceptor + html

    # --- Eliminar X-Frame-Options y CSP del HTML (meta tags) ---
    html = re.sub(
        r'<meta[^>]+http-equiv=["\']?(X-Frame-Options|Content-Security-Policy)["\']?[^>]*>',
        '',
        html,
        flags=re.IGNORECASE
    )

    # --- Neutralizar detección de iframe de SUNAT (self != top / window != top) ---
    html = _neutralize_frame_checks(html)

    # --- Reescribir construcción dinámica de dominio SUNAT en scripts inline del HTML ---
    # SUNAT hace 'https://' + n.sdm + '.sunat.gob.pe' dentro de MenuInternet.htm inline.
    # El branch de JS solo toca archivos .js; aquí cubrimos los <script> embebidos.
    def _rewrite_inline_js_domain(sm):
        sc = sm.group(1)
        sc = re.sub(
            r'''(["'])https://\1\s*\+\s*([\w.[\]]+)\s*\+\s*(["'])\.sunat\.gob\.pe\3''',
            lambda m2: f"('{PROXY_BASE}/proxy/{token}/r?url='+encodeURIComponent('https://'+{m2.group(2)}+'.sunat.gob.pe'))",
            sc
        )
        return sm.group(0).replace(sm.group(1), sc, 1)

    html = re.sub(
        r'<script(?![^>]*\bsrc\b)[^>]*>(.*?)</script>',
        _rewrite_inline_js_domain,
        html,
        flags=re.DOTALL | re.IGNORECASE
    )

    return html


def _neutralize_frame_checks(code: str) -> str:
    """Neutraliza todas las variantes de detección de iframe de SUNAT."""

    # ── PASO 1: Redirigir top.location / parent.location → window.location ──
    # Con esto, cualquier navegación que SUNAT haga via top/parent
    # pasa por nuestro interceptor JS que ya bloquea URLs "saliendo".
    code = re.sub(
        r'\b(?:window\.top|window\.parent|self\.top|top|parent)\.location\b',
        'window.location',
        code
    )

    # ── PASO 2: Variantes negativas en if → if (false) ──
    for pattern in [
        r'if\s*\(\s*(?:self|window(?:\.self)?)\s*!==?\s*(?:top|window\.top|self\.top)\s*\)',
        r'if\s*\(\s*(?:top|window\.top|self\.top)\s*!==?\s*(?:self|window(?:\.self)?)\s*\)',
    ]:
        code = re.sub(pattern, 'if (false)', code)

    # ── PASO 3: Expresión booleana sin if → false ──
    # Cubre: ternarios, &&, asignaciones de variable, new Function(), eval()
    for pattern in [
        r'\bself\s*!==?\s*top\b',
        r'\btop\s*!==?\s*self\b',
        r'\bwindow\s*!==?\s*window\.top\b',
        r'\bwindow\.top\s*!==?\s*window\b',
        r'\bwindow\.self\s*!==?\s*window\.top\b',
        r'\bwindow\.top\s*!==?\s*window\.self\b',
        r'\bwindow\s*!==?\s*top\b',       # variante: window != top
        r'\btop\s*!==?\s*window\b',       # variante: top != window
        r'\bself\s*!==?\s*window\b',      # variante: self != window
        r'\bwindow\s*!==?\s*self\b',      # variante: window != self
    ]:
        code = re.sub(pattern, 'false', code)

    # ── PASO 4: Variantes positivas en if → if (true) ──
    for pattern in [
        r'if\s*\(\s*(?:self|window(?:\.self)?)\s*===?\s*(?:top|window\.top|self\.top)\s*\)',
        r'if\s*\(\s*(?:top|window\.top|self\.top)\s*===?\s*(?:self|window(?:\.self)?)\s*\)',
    ]:
        code = re.sub(pattern, 'if (true)', code)

    # ── PASO 5: Expresión booleana positiva sin if → true ──
    for pattern in [
        r'\bself\s*===?\s*top\b',
        r'\btop\s*===?\s*self\b',
        r'\bwindow\s*===?\s*window\.top\b',
        r'\bwindow\.top\s*===?\s*window\b',
        r'\bwindow\s*===?\s*top\b',        # variante: window == top
        r'\btop\s*===?\s*window\b',        # variante: top == window
    ]:
        code = re.sub(pattern, 'true', code)

    return code


def _rewrite_css(css: str, token: str, base_url: str) -> str:
    """Reescribe URLs dentro de archivos CSS."""
    def rewrite_url(m):
        url = m.group(1).strip("'\"")
        if url.startswith("data:"):
            return m.group(0)
        abs_url = url if url.startswith("http") else urljoin(base_url, url)
        # Activos estáticos: carga directa
        try:
            ext = os.path.splitext(urlparse(abs_url).path)[1].lower()
        except Exception:
            ext = ""
        if ext in STATIC_EXTENSIONS:
            return f'url({abs_url})'
        if any(d in abs_url for d in ["sunat.gob.pe", "sunafil.gob.pe"]):
            return f'url({_make_proxy_url(token, abs_url)})'
        return m.group(0)
    return re.sub(r'url\(([^)]+)\)', rewrite_url, css)


# Headers a eliminar de respuestas de SUNAT (bloquean iframe/CORS)
BLOCKED_RESPONSE_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-content-type-options",
    "strict-transport-security",
    "access-control-allow-origin",
    "transfer-encoding",
    "content-encoding",
    "content-length",
}

def _clean_headers(headers: dict) -> dict:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in BLOCKED_RESPONSE_HEADERS
    }

# ===============================
# PLAYWRIGHT HELPERS
# ===============================

async def _nuevo_context(browser):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        locale="es-PE",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    )
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['es-PE', 'es', 'en-US', 'en'] });
    """)
    page = await context.new_page()
    return context, page


async def _type_human(page, selector: str, text: str):
    import random
    await page.click(selector)
    await page.wait_for_timeout(80)
    for i, char in enumerate(text):
        await page.keyboard.type(char)
        if i < len(text) - 1:
            await page.wait_for_timeout(random.randint(20, 60))


async def _llenar_sol(page, creds: Credenciales):
    await page.wait_for_selector("#txtRuc", state="visible", timeout=15000)
    try:
        if not await page.is_visible("#txtRuc"):
            await page.click("#btnPorRuc")
            await page.wait_for_timeout(400)
    except:
        pass
    await _type_human(page, "#txtRuc", creds.ruc)
    await _type_human(page, "#txtUsuario", creds.usuario_sol.upper())
    await _type_human(page, "#txtContrasena", creds.clave_sol)
    try:
        if await page.is_checked("#chkRecuerdame"):
            await page.uncheck("#chkRecuerdame")
    except:
        pass


async def _login_ok(page) -> bool:
    for sel in ["text=Bienvenido", "a:has-text('Buzón Electrónico')", "text=Favoritos", "#aOpcionBuzon"]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except:
            continue
    return False

# ===============================
# LOGIN CORE
# ===============================

async def _do_login(creds: Credenciales) -> dict:
    rid = str(uuid.uuid4())[:8]
    logger.info(f"[{rid}] Login {creds.portal} | RUC={creds.ruc}")

    global browser
    context = page = None
    try:
        if not browser:
            raise RuntimeError("Playwright/Chromium no inicializado (startup no ejecutado)")

        context, page = await _nuevo_context(browser)

        if creds.portal == "declaracion":
            # Declaración y Pago:
            # - El bot hace login normal en Menú SOL (SUNAT_LOGIN_URL).
            # - La extensión, con esas cookies, abrirá directamente el menú
            #   de Declaraciones usando SUNAT_DECLARACION_REDIRECT_URL (exe=).
            login_url    = SUNAT_LOGIN_URL
            redirect_url = SUNAT_DECLARACION_REDIRECT_URL
            domains      = ["sunat.gob.pe", "e-menu.sunat.gob.pe", "api-seguridad.sunat.gob.pe", "ww1.sunat.gob.pe", "sol.sunat.gob.pe"]
        elif creds.portal == "sunafil":
            login_url    = SUNAFIL_CASILLA_LOGIN_URL
            redirect_url = SUNAFIL_CASILLA_ORIGINAL_URL
            domains      = ["sunat.gob.pe", "sunafil.gob.pe"]
        elif creds.portal == "buzon":
            await page.goto(SUNAT_URL, wait_until="domcontentloaded", timeout=PW_NAV_TIMEOUT)
            await page.wait_for_timeout(600)
            login_url    = SUNAT_LOGIN_URL
            redirect_url = SUNAT_MENU_URL
            domains      = ["sunat.gob.pe", "e-menu.sunat.gob.pe", "api-seguridad.sunat.gob.pe", "ww1.sunat.gob.pe"]
        else:
            await page.goto(SUNAT_URL, wait_until="domcontentloaded", timeout=PW_NAV_TIMEOUT)
            await page.wait_for_timeout(600)
            login_url    = SUNAT_LOGIN_URL
            redirect_url = SUNAT_MENU_URL
            domains      = ["sunat.gob.pe", "e-menu.sunat.gob.pe", "api-seguridad.sunat.gob.pe", "ww1.sunat.gob.pe"]

        resp = await page.goto(login_url, wait_until="domcontentloaded", timeout=PW_NAV_TIMEOUT)
        if not resp or resp.status != 200:
            raise Exception(f"HTTP {resp.status if resp else 'sin respuesta'}")

        await page.wait_for_timeout(1000)
        await _llenar_sol(page, creds)
        await page.click("#btnAceptar")
        await page.wait_for_timeout(800)

        ok = False
        last_err = None
        for _ in range(20):
            try:
                err = page.locator("#divMensajeError, #spanMensajeError")
                if await err.count() > 0 and await err.first.is_visible():
                    try:
                        last_err = await page.inner_text("#spanMensajeError")
                    except:
                        last_err = "Credenciales incorrectas"
                    break
                # SUNAFIL y DECLARACION: detectar por URL (no tienen selectores DOM de SUNAT)
                if creds.portal in ("sunafil", "declaracion"):
                    current_url = page.url.lower()
                    if "login" not in current_url and "oauth2" not in current_url:
                        ok = True
                        break
                # SUNAT Menú SOL: detectar por selectores DOM
                elif await _login_ok(page):
                    ok = True
                    break
            except:
                pass
            await page.wait_for_timeout(600)

        if not ok:
            if last_err:
                return {"ok": False, "error": "credenciales_invalidas", "detalle": _normalize(last_err)}
            return {"ok": False, "error": "login_timeout", "detalle": f"URL: {page.url}"}

        # Esperar que SUNAT termine de generar las cookies base
        await page.wait_for_timeout(1500)

        # Para declaracion ya no hacemos navegación extra a itmenu2 ni al exe
        # desde el bot; solo necesitamos las cookies del Menú SOL. El navegador
        # del usuario será quien abra luego el URL OAuth de Declaración y Pago
        # (SUNAT_DECLARACION_LOGIN_URL) con esas mismas cookies.

        # ── Warm-up visor: SOLO si portal es "buzon" ─────────
        if creds.portal == "buzon":
            try:
                logger.info(f"[{rid}] Warm-up visor via clic en Buzón Electrónico...")
                await page.goto(SUNAT_MENU_URL, wait_until="domcontentloaded", timeout=PW_NAV_TIMEOUT)
                await page.wait_for_timeout(2000)

                # ── Cerrar TODOS los modales/popups que bloquean el click ──
                await page.evaluate("""
                    () => {
                        // 1. Modal de campaña (ifrVCE / divModalCampana)
                        var modalCampana = document.getElementById('divModalCampana');
                        if (modalCampana) modalCampana.style.display = 'none';

                        // 2. Modal "Valida tus datos de contacto"
                        //    Intentar click en "Finalizar" primero (si está visible)
                        var btnFinalizar = document.getElementById('btnFinalizarValidacionDatos');
                        if (btnFinalizar && btnFinalizar.offsetParent !== null) {
                            btnFinalizar.click();
                        }
                        //    Fallback: "Continuar sin confirmar"
                        var btnCerrar = document.getElementById('btnCerrar');
                        if (btnCerrar && btnCerrar.offsetParent !== null) {
                            btnCerrar.click();
                        }

                        // 3. Limpiar cualquier backdrop/overlay genérico de Bootstrap
                        document.querySelectorAll('.modal-backdrop').forEach(function(el) { el.remove(); });
                        document.body.classList.remove('modal-open');
                        document.body.style.overflow = '';
                    }
                """)
                await page.wait_for_timeout(600)
                logger.info(f"[{rid}] Modales cerrados via JS")

                # ── Click en Buzón via JS (evita intercepción del pointer) ──
                clicked = await page.evaluate("""
                    () => {
                        var el = document.getElementById('aOpcionBuzon');
                        if (el) { el.click(); return true; }
                        return false;
                    }
                """)
                logger.info(f"[{rid}] Click en #aOpcionBuzon via JS: {clicked}")
                await page.wait_for_timeout(4000)

                all_c = await context.cookies()
                visor_c = [c for c in all_c if "VISOR" in c["name"].upper()]
                logger.info(f"[{rid}] Cookies VISOR tras clic: {[c['name'] for c in visor_c]}")
            except Exception as e:
                logger.warning(f"[{rid}] Warm-up visor error: {e}")
        # ─────────────────────────────────────────────────────────────

        all_cookies = await context.cookies()
        cookies = _filter_cookies(all_cookies, domains)
        cookie_names = [f"{c['name']}@{c['domain']}" for c in cookies]
        logger.info(f"[{rid}] Login OK | cookies={len(cookies)} | names={cookie_names}")

        return {
            "ok": True, "portal": creds.portal, "ruc": creds.ruc,
            "redirect_url": redirect_url, "cookies": cookies, "request_id": rid,
        }

    except Exception as e:
        logger.exception(f"[{rid}] Error: {e}")
        return {"ok": False, "error": "error_inesperado", "detalle": str(e), "request_id": rid}

    finally:
        for obj, method in [(page, "close"), (context, "close")]:
            try:
                if obj:
                    await getattr(obj, method)()
            except:
                pass
        logger.info(f"[{rid}] Página/Context cerrado ✅")

# ===============================
# ENDPOINTS
# ===============================

@app.post("/proxy/create")
async def proxy_create(creds: Credenciales):
    """Inicia login en background y devuelve token inmediatamente.
    Usar GET /proxy/status/{token} para saber cuándo está listo.
    """
    _cleanup_proxy_sessions()

    token = uuid.uuid4().hex
    # Guardar sesión como 'pending' antes de hacer login
    proxy_sessions[token] = {
        "status":       "pending",
        "cookies":      [],
        "redirect_url": None,
        "portal":       creds.portal,
        "ruc":          creds.ruc,
        "expires_at":   datetime.now() + timedelta(minutes=PROXY_TTL_MIN),
        "error":        None,
    }
    _save_sessions_to_disk()
    logger.info(f"[{token[:8]}] Login iniciado en background...")

    # Lanzar login en background sin bloquear la respuesta HTTP
    asyncio.create_task(_login_background(token, creds))

    return {
        "ok":      True,
        "token":   token,
        "status":  "pending",
        "portal":  creds.portal,
        "ruc":     creds.ruc,
        "proxy_url": f"{PROXY_BASE}/proxy/{token}",
        "expires_in_minutes": PROXY_TTL_MIN,
    }


async def _login_background(token: str, creds: Credenciales):
    """Corre el login de Playwright en segundo plano y actualiza la sesión."""
    try:
        result = await _do_login(creds)
        session = proxy_sessions.get(token)
        if not session:
            return  # sesión fue eliminada mientras esperaba
        if result.get("ok"):
            session["status"]       = "ready"
            session["cookies"]      = result["cookies"]
            session["redirect_url"] = result["redirect_url"]
            logger.info(f"[{token[:8]}] Login OK — sesión lista")
        else:
            session["status"] = "error"
            session["error"]  = result.get("error", "unknown")
            session["detalle"] = result.get("detalle", "")
            logger.warning(f"[{token[:8]}] Login FAILED: {session['error']}")
        _save_sessions_to_disk()
    except Exception as e:
        logger.exception(f"[{token[:8]}] Login background exception: {e}")
        session = proxy_sessions.get(token)
        if session:
            session["status"] = "error"
            session["error"]  = "error_inesperado"
            session["detalle"] = str(e)
            _save_sessions_to_disk()


@app.get("/proxy/status/{token}")
async def proxy_status(token: str):
    """Polling endpoint — devuelve el estado del login en background.
    status: 'pending' | 'ready' | 'error'
    """
    session = proxy_sessions.get(token)
    if not session:
        return JSONResponse(status_code=404, content={"ok": False, "status": "not_found"})
    if session["expires_at"] <= datetime.now():
        return JSONResponse(status_code=410, content={"ok": False, "status": "expired"})

    status = session.get("status", "pending")
    if status == "ready":
        return {"ok": True, "status": "ready",
                "ext_inject_url": f"{PROXY_BASE}/ext-inject/{token}"}
    if status == "error":
        return JSONResponse(status_code=422, content={
            "ok": False, "status": "error",
            "error": session.get("error"),
            "detalle": session.get("detalle", ""),
        })
    return {"ok": True, "status": "pending"}


@app.get("/get-cookies/{token}")
async def get_cookies(token: str):
    """Devuelve las cookies de sesión como JSON para la extensión Chrome.
    No requiere API key — el token es la credencial.
    """
    _cleanup_proxy_sessions()
    session = proxy_sessions.get(token)
    if not session or session["expires_at"] <= datetime.now():
        if session:
            del proxy_sessions[token]
        return JSONResponse(status_code=410, content={"ok": False, "error": "session_expired"})

    if session.get("status", "ready") == "pending":
        return JSONResponse(status_code=202, content={"ok": False, "error": "login_pending"})
    if session.get("status") == "error":
        return JSONResponse(status_code=422, content={"ok": False, "error": session.get("error", "login_error")})

    logger.info(f"[GetCookies] token={token[:8]}... cookies={len(session['cookies'])}")
    return JSONResponse(
        content={
            "ok": True,
            "cookies": session["cookies"],
            "redirect_url": session.get("redirect_url",
                "https://e-menu.sunat.gob.pe/cl-ti-itmenu/MenuInternet.htm"),
        },
        headers={"ngrok-skip-browser-warning": "true"},
    )


@app.get("/ext-inject/{token}", response_class=HTMLResponse)
async def ext_inject(token: str):
    """Página de aterrizaje para la extensión Chrome.
    La extensión intercepta esta URL, inyecta cookies y redirige a SUNAT.
    Si la extensión no está instalada, muestra instrucciones.
    """
    _cleanup_proxy_sessions()
    session = proxy_sessions.get(token)
    if not session or session["expires_at"] <= datetime.now():
        if session:
            del proxy_sessions[token]
        return HTMLResponse(
            "<h2>Sesión expirada. Cierre e intente nuevamente.</h2>",
            status_code=410
        )

    logger.info(f"[ExtInject] token={token[:8]}...")
    html = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Conectando con SUNAT...</title>
  <style>
    body{font-family:'Segoe UI',sans-serif;display:flex;align-items:center;
         justify-content:center;height:100vh;margin:0;background:#f0f4f8;}
    .card{background:#fff;border-radius:12px;padding:40px 48px;text-align:center;
          box-shadow:0 4px 24px rgba(0,0,0,.1);max-width:380px;}
    .spinner{width:44px;height:44px;border:4px solid #e0e0e0;
             border-top-color:#1a73e8;border-radius:50%;
             animation:spin .8s linear infinite;margin:0 auto 20px;}
    @keyframes spin{to{transform:rotate(360deg)}}
    h2{margin:0 0 8px;color:#202124;font-size:19px;}
    p{color:#5f6368;font-size:14px;margin:0 0 16px;}
    .warn{font-size:12px;color:#b45309;margin-top:16px;padding:10px 12px;
          background:#fef3c7;border-radius:8px;line-height:1.5;}
    a{color:#1a73e8;}
  </style>
</head>
<body>
  <div class="card">
    <div class="spinner"></div>
    <h2>Conectando con SUNAT...</h2>
    <p>La extensión está inyectando tu sesión.</p>
    <div class="warn">
      Si no eres redirigido en 5 segundos, asegúrate de tener instalada
      la extensión <strong>SUNAT Session Injector</strong> en Chrome.
    </div>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=html, headers={"ngrok-skip-browser-warning": "true"})


@app.get("/session-redirect/{token}")
async def session_redirect(token: str):
    """
    Inyecta las cookies de sesión SUNAT en el browser del cliente
    y redirige directo a MenuInternet.htm (sin pasar por el proxy).
    Útil para abrir la sesión en una pestaña real del navegador.
    """
    _cleanup_proxy_sessions()
    session = proxy_sessions.get(token)

    if not session or session["expires_at"] <= datetime.now():
        if session:
            del proxy_sessions[token]
        return JSONResponse(status_code=410, content={"ok": False, "error": "session_expired"})

    portal = session.get("portal", "sunat")

    # Redirect URL según portal
    redirect_location = session.get("redirect_url") or (
        SUNAFIL_CASILLA_ORIGINAL_URL if portal == "sunafil"
        else "https://e-menu.sunat.gob.pe/cl-ti-itmenu/MenuInternet.htm"
    )
    headers = {"Location": redirect_location}

    # Construir Set-Cookie por cada cookie de la sesión
    set_cookie_values = []
    for c in session["cookies"]:
        name  = c.get("name", "")
        value = c.get("value", "")
        path  = c.get("path") or "/"
        # Dominio raíz según si la cookie es de SUNAFIL o SUNAT
        c_domain = c.get("domain", "")
        if "sunafil" in c_domain:
            root_domain = ".sunafil.gob.pe"
        else:
            root_domain = ".sunat.gob.pe"
        cookie_str = f"{name}={value}; Domain={root_domain}; Path={path}; SameSite=None; Secure"
        if c.get("httpOnly"):
            cookie_str += "; HttpOnly"
        set_cookie_values.append(cookie_str)

    response = Response(content=b"", status_code=302, headers=headers)
    for cookie_str in set_cookie_values:
        response.headers.append("Set-Cookie", cookie_str)

    logger.info(f"[SessionRedirect] token={token[:8]}... cookies={len(set_cookie_values)}")
    return response


@app.get("/proxy/{token}", response_class=HTMLResponse)
async def proxy_serve_root(token: str):
    """Sirve la página principal de SUNAT autenticada."""
    _cleanup_proxy_sessions()
    session = proxy_sessions.get(token)

    if not session or session["expires_at"] <= datetime.now():
        if session:
            del proxy_sessions[token]
        return HTMLResponse(_error_html("Sesión expirada. Cierre e intente nuevamente."), status_code=410)

    return await _proxy_fetch(token, session, session["redirect_url"], "GET", None)


@app.api_route("/proxy/{token}/r", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_resource(token: str, request: Request):
    """
    Proxy universal: sirve cualquier recurso de SUNAT.
    ?url=<url_encoded> → recurso a proxear
    """
    # OPTIONS: responder inmediatamente con CORS (preflight)
    if request.method == "OPTIONS":
        return Response(
            content=b"",
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            },
        )
    _cleanup_proxy_sessions()
    session = proxy_sessions.get(token)

    if not session or session["expires_at"] <= datetime.now():
        return Response(content=b"Session expired", status_code=410)

    target_url = request.query_params.get("url")
    if not target_url:
        return Response(content=b"Missing url param", status_code=400)

    target_url = unquote(target_url)
    logger.info(f"[Proxy] {request.method} → {target_url}")

    # Bloquear URLs que apunten al propio proxy (evita loop infinito por doble-proxeado)
    if PROXY_BASE and target_url.startswith(PROXY_BASE):
        return Response(content=b"Self-referential URL blocked", status_code=400)

    # Seguridad: solo proxear dominios de SUNAT (verificar hostname, no toda la URL)
    parsed_target = urlparse(target_url)
    if not any(d in parsed_target.netloc for d in ["sunat.gob.pe", "sunafil.gob.pe"]):
        return Response(content=b"Domain not allowed", status_code=403)

    # Activos estáticos: redirigir directo a SUNAT (evita saturar ngrok)
    try:
        ext = os.path.splitext(urlparse(target_url).path)[1].lower()
    except Exception:
        ext = ""
    if ext in STATIC_EXTENSIONS:
        return Response(
            status_code=302,
            headers={"Location": target_url, "Cache-Control": "public, max-age=86400"},
            content=b"",
        )

    # Bloquear páginas de "saliendo" por URL — devolver página en blanco (NO redirect: evita loop)
    _SALIENDO_URL_PATTERNS = ["saliendo", "cerrar_ventana", "cerrarventana", "mensajesalida", "salidamenu", "salida.htm"]
    if any(s in target_url.lower() for s in _SALIENDO_URL_PATTERNS):
        logger.info(f"[Proxy] Saliendo URL blocked: {target_url}")
        return Response(
            content=b"<html><head></head><body></body></html>",
            media_type="text/html; charset=utf-8",
            status_code=200,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    body = None
    if request.method in ["POST", "PUT"]:
        body = await request.body()

    return await _proxy_fetch(token, session, target_url, request.method, body, request.headers)


async def _proxy_fetch(
    token: str,
    session: dict,
    target_url: str,
    method: str = "GET",
    body: bytes = None,
    req_headers: dict = None,
) -> Response:
    """Hace el request a SUNAT y devuelve la respuesta procesada."""
    cookie_header = _cookies_to_header(session["cookies"])

    u = urlparse(target_url)
    origin = f"{u.scheme}://{u.netloc}"

    headers = {
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "*/*",
        "Accept-Language": "es-PE,es;q=0.9",
        "Origin": origin,
        "Referer": origin + "/",
    }

    # Pasar Content-Type si viene del request original
    if req_headers:
        ct = req_headers.get("content-type")
        if ct:
            headers["Content-Type"] = ct

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            verify=False,
        ) as client:
            resp = await client.request(
                method=method,
                url=target_url,
                headers=headers,
                content=body,
            )

        # Log de status
        logger.info(f"[Proxy] {method} {target_url} → {resp.status_code}")
        if resp.status_code >= 400:
            logger.error(f"[Proxy] {method} {target_url} → {resp.status_code}")
            logger.error(f"[Proxy] body: {resp.text[:500]}")

        # Verificar si la cadena de redirects terminó en una página de salida
        final_url = str(resp.url)
        if any(s in final_url.lower() for s in ["saliendo", "cerrar_ventana", "cerrarventana"]):
            return Response(
                status_code=302,
                headers={"Location": f"{PROXY_BASE}/proxy/{token}"},
                content=b"",
            )

        content_type = resp.headers.get("content-type", "")
        clean_headers = _clean_headers(dict(resp.headers))

        # Agregar headers CORS para que el iframe funcione
        clean_headers["Access-Control-Allow-Origin"] = "*"
        clean_headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        clean_headers["Access-Control-Allow-Headers"] = "*"

        # Procesar según tipo de contenido
        if "text/html" in content_type:
            html = resp.text
            # ── Detección por CONTENIDO: SUNAT puede servir saliendo desde cualquier URL ──
            # IMPORTANTE: quitar <script> antes de chequear — el menú legítimo contiene
            # el texto "saliendo del Menú SOL" dentro de su código JS de detección de iframe.
            # Solo queremos detectarlo en el contenido HTML visible (body), no en scripts.
            _html_no_scripts = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            _html_lower = _html_no_scripts.lower()
            # ⚠️ IMPORTANTE: ser MUY específico — muchas páginas SUNAT legítimas tienen
            # botones "Cerrar esta ventana", "Salir", etc.
            # Solo bloquear cuando hay señales INEQUÍVOCAS de la página de salida del menú.
            _SALIENDO_SIGNALS = [
                "saliendo del men",          # «saliendo del Menú SOL» / «saliendo del Menú Internet»
                "debe cerrar esta ventana",  # frase exacta de la página saliendo (con "debe")
                "está saliendo",             # «Está saliendo del sistema»
                "esta saliendo",             # variante sin tilde
                "mensajesalida",             # id/class específico de SUNAT
                "salida del men",            # «salida del menú»
            ]
            _matched_signal = next((s for s in _SALIENDO_SIGNALS if s in _html_lower), None)
            if _matched_signal:
                logger.info(f"[Proxy] Saliendo page blocked by content signal='{_matched_signal}': {target_url}")
                # Devolver página en blanco — NO redirigir al menú (evita loop)
                return Response(
                    content=b"<html><head></head><body></body></html>",
                    media_type="text/html; charset=utf-8",
                    status_code=200,
                    headers={"Access-Control-Allow-Origin": "*"},
                )
            html = _rewrite_html(html, token, target_url)
            return Response(
                content=html.encode("utf-8", errors="replace"),
                media_type="text/html; charset=utf-8",
                headers=clean_headers,
            )

        elif "text/css" in content_type:
            css = resp.text
            css = _rewrite_css(css, token, target_url)
            return Response(
                content=css.encode("utf-8", errors="replace"),
                media_type="text/css; charset=utf-8",
                headers=clean_headers,
            )

        elif "application/json" in content_type or "text/json" in content_type or (
            target_url.endswith(".json") and ("json" in content_type or "octet" in content_type or not content_type)
        ):
            # JSON: reescribir dominio SUNAT en dominios.json para que navegación pase por proxy
            raw_json = resp.text
            if "dominios" in target_url.lower() or "dominio" in target_url.lower():
                try:
                    data = json.loads(raw_json)
                    items = data.get("items") or data.get("dominios") or []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        # Reconstruir la URL real que SUNAT usaría
                        # SUNAT hace: 'https://' + sdm + '.sunat.gob.pe'
                        sdm = item.get("sdm") or item.get("subdominio") or ""
                        if sdm:
                            real_domain = f"https://{sdm}.sunat.gob.pe"
                            # Reemplazar por proxy URL — SUNAT concatena dominio + path,
                            # así frmContenido.location.href = proxyUrl + path
                            # = /proxy/TOKEN/r?url=https%3A%2F%2Fww1.sunat.gob.pe  +  /fichacruc.htm
                            # = /proxy/TOKEN/r?url=https%3A%2F%2Fww1.sunat.gob.pe/fichacruc.htm  ✅
                            proxy_domain = _make_proxy_url(token, real_domain).rstrip("/")
                            # NO tocar item["sdm"] — SUNAT lo usa para construir el dominio
                            # en JS; el regex inline transform lo interceptará correctamente.
                            # Inyectar campo dominio directo (por si SUNAT lo lee desde JSON)
                            item["dominio"] = proxy_domain
                        if "dominio" in item and isinstance(item["dominio"], str) and "sunat.gob.pe" in item["dominio"]:
                            item["dominio"] = _make_proxy_url(token, item["dominio"]).rstrip("/")
                    raw_json = json.dumps(data)
                    logger.info(f"[Proxy] dominios.json reescrito: {len(items)} items")
                except Exception as e:
                    logger.warning(f"[Proxy] Error reescribiendo dominios.json: {e}")
            return Response(
                content=raw_json.encode("utf-8", errors="replace"),
                media_type="application/json; charset=utf-8",
                headers=clean_headers,
            )

        elif "javascript" in content_type or "ecmascript" in content_type:
            # JS: reescribir URLs de SUNAT hardcodeadas + neutralizar checks de iframe
            js = resp.text
            for domain in [
                "e-menu.sunat.gob.pe", "www.sunat.gob.pe", "ww1.sunat.gob.pe",
                "sol.sunat.gob.pe", "ww4.sunat.gob.pe", "ww3.sunat.gob.pe",
                "api.sunat.gob.pe", "api-seguridad.sunat.gob.pe",
                "orientacion.sunat.gob.pe",
            ]:
                js = js.replace(
                    f"https://{domain}",
                    f"{PROXY_BASE}/proxy/{token}/r?url=https://{domain}"
                )
            # ★ Reescribir construcción dinámica de dominio SUNAT: 'https://'+sdm+'.sunat.gob.pe'
            # SUNAT lee sdm del JSON y construye el dominio así en el JS del menú.
            # Patrón: ['"]https://['"\s]*+\s*VAR\s*+\s*['"]\.sunat\.gob\.pe['"]
            # Lo reemplazamos por una función que devuelve la URL proxeada.
            js = re.sub(
                r'''(["'])https://\1\s*\+\s*([\w.[\]]+)\s*\+\s*(["'])\.sunat\.gob\.pe\3''',
                lambda m: f"('{PROXY_BASE}/proxy/{token}/r?url='+encodeURIComponent('https://'+{m.group(2)}+'.sunat.gob.pe'))",
                js
            )
            js = _neutralize_frame_checks(js)
            return Response(
                content=js.encode("utf-8", errors="replace"),
                media_type=content_type,
                headers=clean_headers,
            )

        else:
            # Binario: fuentes, imágenes, etc — pasar tal cual
            return Response(
                content=resp.content,
                media_type=content_type or "application/octet-stream",
                headers=clean_headers,
            )

    except Exception as e:
        logger.error(f"[Proxy] Error fetching {target_url}: {e}")
        return Response(content=f"Proxy error: {e}".encode(), status_code=502)


def _error_html(msg: str) -> str:
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
    <style>
        body{{font-family:sans-serif;display:flex;align-items:center;
             justify-content:center;min-height:100vh;margin:0;background:#f8fafc}}
        .box{{background:white;border-radius:12px;padding:32px;text-align:center;
              box-shadow:0 4px 16px rgba(0,0,0,.08);max-width:380px}}
        h3{{color:#1e3a5f;margin-bottom:8px}} p{{color:#64748b;font-size:.9rem}}
    </style></head><body>
    <div class="box"><div style="font-size:2.5rem;margin-bottom:16px">⚠️</div>
    <h3>Error de sesión</h3><p>{msg}</p></div></body></html>"""


@app.post("/session")
async def get_session(creds: Credenciales):
    """Devuelve cookies raw (uso avanzado)."""
    result = await _do_login(creds)
    if result.get("ok"):
        return result
    return JSONResponse(
        status_code=401 if result.get("error") == "credenciales_invalidas" else 500,
        content=result
    )


@app.get("/health")
async def health():
    _cleanup_proxy_sessions()
    return {
        "ok": True,
        "service": "Bot Cookies SUNAT + SUNAFIL v3",
        "version": "3.1.0",
        "proxy_sessions_activas": len(proxy_sessions),
        "time": datetime.now().isoformat(timespec="seconds"),
    }


@app.post("/declaracion/proxy/create")
async def declaracion_proxy_create(creds: Credenciales):
    """Alias explícito para login portal Declaración y Pago SUNAT.
    Equivalente a POST /proxy/create con portal='declaracion'.
    """
    creds.portal = "declaracion"
    return await proxy_create(creds)


@app.post("/sunafil/proxy/create")
async def sunafil_proxy_create(creds: Credenciales):
    """Alias explícito para login SUNAFIL Casilla Electrónica.
    Equivalente a POST /proxy/create con portal='sunafil'.
    """
    creds.portal = "sunafil"
    return await proxy_create(creds)


@app.post("/buzon/create")
async def buzon_create(creds: Credenciales):
    """Login específico para el buzón — incluye warm-up del visor."""
    creds.portal = "buzon"
    return await proxy_create(creds)


@app.get("/")
async def root():
    return {
        "servicio": "Bot Cookies SUNAT v3",
        "flujo": "POST /proxy/create → abrir proxy_url en iframe",
        "endpoints": {
            "POST /proxy/create":              "Login + genera token (portal: sunat|declaracion|sunafil)",
            "POST /declaracion/proxy/create":  "Login Declaración y Pago (alias explícito)",
            "POST /sunafil/proxy/create":      "Login SUNAFIL Casilla (alias explícito)",
            "POST /buzon/create":              "Login Buzón SOL (alias, incluye warm-up visor)",
            "GET  /proxy/status/{token}":      "Estado del login en background",
            "GET  /proxy/{token}":             "Sirve portal autenticado (página principal)",
            "ANY  /proxy/{token}/r":           "Proxy universal de recursos SUNAT/SUNAFIL",
            "GET  /ext-inject/{token}":        "Página para extensión Chrome (inyección de cookies)",
            "GET  /get-cookies/{token}":       "Devuelve cookies raw para extensión",
            "GET  /session-redirect/{token}":  "Redirige con cookies inyectadas (pestaña real)",
            "POST /session":                   "Devuelve cookies raw (uso avanzado)",
            "GET  /health":                    "Estado del servicio",
        },
        "portales_soportados": {
            "sunat":       "Menú SOL (e-menu.sunat.gob.pe)",
            "declaracion": "Declaración y Pago (api-seguridad.sunat.gob.pe)",
            "sunafil":     "Casilla Electrónica SUNAFIL (casillaelectronica.sunafil.gob.pe)",
            "buzon":      "Buzón SOL (warm-up visor)",
        },
    }


@app.get("/buzon/{token}")
async def buzon_listar(token: str, page: int = 1, todo: bool = False, tipo: int = 2, desde: str = ""):
    session = proxy_sessions.get(token)
    if not session:
        return JSONResponse(status_code=404, content={"ok": False, "error": "token_not_found"})
    if session["expires_at"] <= datetime.now():
        return JSONResponse(status_code=410, content={"ok": False, "error": "expired"})

    from datetime import datetime as dt

    cookies = {c["name"]: c["value"] for c in session["cookies"]}
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "es-ES,es;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://ww1.sunat.gob.pe",
        "Referer": "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/master",
        "Cookie": cookie_header,
    }

    # ── Warm-up: visitar el visor para obtener ITVISORNOTISESSION ──
    async with httpx.AsyncClient(follow_redirects=True, timeout=20, verify=False) as warmup_client:
        warmup_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9",
            "Referer": "https://e-menu.sunat.gob.pe/cl-ti-itmenu/MenuInternet.htm?pestana=*&agrupacion=*",
            "Cookie": cookie_header,
        }
        # Probar las URLs candidatas del visor hasta encontrar una que funcione
        for warmup_url in [
            "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/master",  # URL real actual
            "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/",
            "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/index.jsp",
        ]:
            try:
                wr = await warmup_client.get(warmup_url, headers=warmup_headers)
                logger.info(f"[Buzon] Warm-up {warmup_url} → {wr.status_code} | cookies nuevas: {list(wr.cookies.keys())}")
                # Absorber cookies nuevas
                for name, value in wr.cookies.items():
                    cookies[name] = value
                cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
                headers["Cookie"] = cookie_header
                warmup_headers["Cookie"] = cookie_header
                if wr.status_code == 200:
                    logger.info(f"[Buzon] Warm-up OK en {warmup_url}")
                    break
            except Exception as e:
                logger.warning(f"[Buzon] Warm-up error: {e}")
    # ──────────────────────────────────────────────────────────────

    todos_mensajes = []
    pagina = page
    data = {}
    fecha_desde = None

    if desde:
        try:
            fecha_desde = dt.strptime(desde, "%Y-%m-%d")
        except Exception:
            pass

    async with httpx.AsyncClient(follow_redirects=True, timeout=20, verify=False) as client:
        while True:
            params = {
                "tipoMsj": tipo, "codCarpeta": "00", "codEtiqueta": "",
                "page": pagina, "des_asunto": "", "codMensaje": "",
                "tipoOrden": "NADA",
                "_": str(int(datetime.now().timestamp() * 1000)),
            }
            r = await client.get(
                "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/listNotiMenPag",
                params=params, headers=headers,
            )

            logger.info(f"[Buzon] HTTP {r.status_code} | URL final: {r.url}")
            logger.info(f"[Buzon] Content-Type: {r.headers.get('content-type', '')}")
            logger.info(f"[Buzon] Respuesta: {r.text[:500]}")
            logger.info(f"[Buzon] Cookies enviadas: {list(cookies.keys())}")

            data = r.json()
            rows = data.get("rows")

            if rows is None:
                return JSONResponse(content={
                    "ok": False, "error": "rows_null",
                    "detalle": "Sesión inválida o RUC sin buzón",
                    "ruc": session.get("ruc"),
                    "debug": {
                        "status": r.status_code,
                        "url_final": str(r.url),
                        "content_type": r.headers.get("content-type", ""),
                        "respuesta": r.text[:500],
                    }
                })

            # ── Filtrar por fecha si se especificó "desde" ──────────────
            if fecha_desde:
                rows_validos = []
                hay_anteriores = False
                for m in rows:
                    try:
                        fec = dt.strptime(m.get("fecEnvio", "01/01/1900"), "%d/%m/%Y")
                        if fec >= fecha_desde:
                            rows_validos.append(m)
                        else:
                            hay_anteriores = True
                    except Exception:
                        rows_validos.append(m)
                todos_mensajes.extend(rows_validos)
                # Si encontramos mensajes anteriores → parar paginación
                if hay_anteriores:
                    break
            else:
                todos_mensajes.extend(rows)
            # ────────────────────────────────────────────────────────────

            if not todo or len(rows) < 25:
                break
            pagina += 1

    return {
        "ok": True,
        "ruc": session.get("ruc"),
        "total_obtenidos": len(todos_mensajes),
        "total_buzon": data.get("records", 0),
        "pagina": pagina,
        "mensajes": todos_mensajes,
    }


@app.get("/buzon/{token}/detalle/{codigo_mensaje}")
async def buzon_detalle(token: str, codigo_mensaje: int):
    session = proxy_sessions.get(token)
    if not session:
        return JSONResponse(status_code=404, content={"ok": False, "error": "token_not_found"})
    if session["expires_at"] <= datetime.now():
        return JSONResponse(status_code=410, content={"ok": False, "error": "expired"})

    cookies = {c["name"]: c["value"] for c in session["cookies"]}
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "es-ES,es;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://ww1.sunat.gob.pe",
        "Referer": "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/master",
        "Cookie": cookie_header,
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=20, verify=False) as client:
        r = await client.get(
            "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/obtenerDetalleNotiMen",
            params={
                "codigoMensaje": codigo_mensaje, "tipoMsj": 2,
                "_": str(int(datetime.now().timestamp() * 1000)),
            },
            headers=headers,
        )
    data = r.json()

    msj_raw = data.get("msjMensaje", "")
    if msj_raw and isinstance(msj_raw, str):
        try:
            data["msjMensaje_parsed"] = json.loads(msj_raw)
        except Exception:
            data["msjMensaje_parsed"] = msj_raw

    adjuntos = data.get("listAttach", [])
    data["adjuntos_clasificados"] = {
        "documento_html": next(
            ({"numId": a.get("numId")} for a in adjuntos if str(a.get("indMensaje")) == "3"),
            None
        ),
        "archivos_pdf": [
            {
                "codArchivo": a.get("codArchivo"),
                "nomArchivo": a.get("nomArchivo"),
                "tamano": a.get("tamanoArchivoFormat"),
                "bytes": a.get("cntTamarch"),
            }
            for a in adjuntos if str(a.get("indMensaje")) == "2"
        ],
    }

    return {"ok": True, "ruc": session.get("ruc"), "detalle": data}


@app.get("/buzon/{token}/documento/{cod_mensaje}")
async def buzon_documento(token: str, cod_mensaje: int):
    """Descarga el documento HTML principal del mensaje."""
    session = proxy_sessions.get(token)
    if not session:
        return JSONResponse(status_code=404, content={"ok": False, "error": "token_not_found"})
    if session["expires_at"] <= datetime.now():
        return JSONResponse(status_code=410, content={"ok": False, "error": "expired"})

    cookies = {c["name"]: c["value"] for c in session["cookies"]}
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers_base = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9",
        "Referer": "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/master",
        "Cookie": cookie_header,
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=30, verify=False) as client:
        # Obtener detalle para sacar la URL del documento
        r = await client.get(
            "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/obtenerDetalleNotiMen",
            params={"codigoMensaje": cod_mensaje, "tipoMsj": 2,
                    "_": str(int(datetime.now().timestamp() * 1000))},
            headers={**headers_base, "Accept": "application/json, text/javascript, */*; q=0.01",
                     "X-Requested-With": "XMLHttpRequest"},
        )
        detalle = r.json()
        url_doc = detalle.get("url", "")

        if not url_doc:
            return JSONResponse(status_code=404, content={"ok": False, "error": "sin_url_documento"})

        full_url = f"https://ww1.sunat.gob.pe{url_doc}" if url_doc.startswith("/") else url_doc
        rd = await client.get(full_url, headers={
            **headers_base,
            "Accept": "text/html,application/xhtml+xml,*/*",
        })

    return Response(
        content=rd.content,
        media_type=rd.headers.get("content-type", "text/html"),
        headers={"Content-Disposition": f"inline; filename=doc_{cod_mensaje}.html"},
    )


@app.get("/buzon/{token}/pdf/{cod_mensaje}/{cod_archivo}")
async def buzon_pdf(token: str, cod_mensaje: int, cod_archivo: int):
    """Descarga el PDF adjunto de un mensaje."""
    session = proxy_sessions.get(token)
    if not session:
        return JSONResponse(status_code=404, content={"ok": False, "error": "token_not_found"})
    if session["expires_at"] <= datetime.now():
        return JSONResponse(status_code=410, content={"ok": False, "error": "expired"})

    cookies = {c["name"]: c["value"] for c in session["cookies"]}
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    url = (
        f"https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/obtenerArchivo"
        f"?codArchivo={cod_archivo}&nomArchivo=adjunto_{cod_mensaje}"
    )

    async with httpx.AsyncClient(follow_redirects=True, timeout=30, verify=False) as client:
        r = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/pdf,application/octet-stream,*/*",
            "Accept-Language": "es-ES,es;q=0.9",
            "Referer": "https://ww1.sunat.gob.pe/ol-ti-itvisornoti/visor/master",
            "Cookie": cookie_header,
        })

    nom = f"doc_{cod_mensaje}_{cod_archivo}.pdf"
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "application/pdf"),
        headers={"Content-Disposition": f"attachment; filename={nom}"},
    )


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("COOKIES_HOST", "127.0.0.1")
    port = int(os.environ.get("COOKIES_PORT", "8001"))
    uvicorn.run(app, host=host, port=port)