"""
Instalador Docker CLI + IntelliJ Config
========================================
Baixa os binarios oficiais do Docker diretamente das fontes:
  - Docker Engine: https://download.docker.com/win/static/stable/x86_64/
  - Docker Compose: https://github.com/docker/compose/releases
  - Docker Buildx:  https://github.com/docker/buildx/releases

com menu interativo para escolha de versao, e configura o IntelliJ IDEA
para usar o Docker local via npipe.
"""

import shutil
import os
import glob
import ctypes
import sys
import urllib.request
import urllib.error
import zipfile
import re
import json
import tempfile
import traceback
import time
import threading
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# Cores ANSI (Windows 10+ build 16257+ suporta nativamente)
# ---------------------------------------------------------------------------

def _enable_vt100() -> None:
    """Habilita sequencias de escape ANSI no terminal Windows."""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass

_enable_vt100()

C = {
    "rst":    "\033[0m",
    "bold":   "\033[1m",
    "dim":    "\033[2m",
    "red":    "\033[91m",
    "green":  "\033[92m",
    "yellow": "\033[93m",
    "blue":   "\033[94m",
    "magenta":"\033[95m",
    "cyan":   "\033[96m",
    "white":  "\033[97m",
    "bg_red":   "\033[41m",
    "bg_green": "\033[42m",
    "bg_blue":  "\033[44m",
    "bg_cyan":  "\033[46m",
}

def c(color: str, text: str) -> str:
    return f"{C.get(color, '')}{text}{C['rst']}"

OK   = lambda t: c("green", t)
WARN = lambda t: c("yellow", t)
ERR  = lambda t: c("red", t)
INFO = lambda t: c("cyan", t)
HDR  = lambda t: c("bold", t) + c("magenta", t)
DIM  = lambda t: c("dim", t)

ICONS = {"ok": OK("✓"), "err": ERR("✗"), "warn": WARN("⚠"), "info": INFO("▶"),
         "gear": "⚙", "box": "📦", "ship": "🚀", "disk": "💾", "net": "🌐"}

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
DOCKER_STATIC_BASE = "https://download.docker.com/win/static/stable/x86_64/"
DOCKER_COMPOSE_API = "https://api.github.com/repos/docker/compose/releases"
DOCKER_BUILDX_API  = "https://api.github.com/repos/docker/buildx/releases"

# ---------------------------------------------------------------------------
# Helpers de sistema
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")

def maximize_console() -> None:
    """Maximiza a janela do console no Windows."""
    if os.name == "nt":
        try:
            SW_MAXIMIZE = 3
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Spinner animado para operacoes longas
# ---------------------------------------------------------------------------

class Spinner:
    """Spinner animado que roda em thread separada."""
    def __init__(self, message: str = ""):
        self.message = message
        self._running = False
        self._thread: threading.Thread | None = None
        self._frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def start(self, message: str = "") -> None:
        if message:
            self.message = message
        self._running = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        i = 0
        while self._running:
            frame = c("cyan", self._frames[i % len(self._frames)])
            print(f"\r  {frame}  {self.message}", end="", flush=True)
            time.sleep(0.08)
            i += 1

    def stop(self, final: str = "") -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.3)
        if final:
            print(f"\r  {final}" + " " * 40)
        else:
            print("\r" + " " * 60, end="\r")

# ---------------------------------------------------------------------------
# Headers e separadores estilizados
# ---------------------------------------------------------------------------

def _print_header(title: str, color: str = "magenta") -> None:
    w = 66
    top = c(color, "╔" + "═" * (w - 2) + "╗")
    mid = c(color, "║") + f"  {title}".center(w - 2) + c(color, "║")
    bot = c(color, "╚" + "═" * (w - 2) + "╝")
    print(f"\n  {top}")
    print(f"  {mid}")
    print(f"  {bot}")
    print()

def _step_header(step: int, title: str) -> None:
    print(f"\n  {c('cyan', f'[{step}/3]')} {c('bold', title)}")
    print(f"  {DIM('─' * 50)}")

def _summary_line(label: str, value: str, icon: str = "  ") -> None:
    print(f"  {icon}  {label}: {c('green', value)}")

# ---------------------------------------------------------------------------
# Download com spinner + barra de progresso colorida
# ---------------------------------------------------------------------------

def download_file(url: str, dest: str, label: str = "") -> bool:
    """Baixa arquivo com barra de progresso colorida no terminal."""
    spinner = Spinner()
    try:
        print(f"\n  {ICONS['net']}  {c('bold', label)}")
        print(f"  {DIM('URL: ' + url)}")
        spinner.start("Conectando...")

        def _progress(block_count, block_size, total_size):
            spinner.stop("")
            if total_size <= 0:
                return
            downloaded = min(block_count * block_size, total_size)
            pct = downloaded * 100 // total_size
            bar_len = 36
            filled = bar_len * downloaded // total_size
            bar_fill = c("green", "█" * filled)
            bar_empty = DIM("░" * (bar_len - filled))
            mb_dl = downloaded / (1024 * 1024)
            mb_total = total_size / (1024 * 1024)
            speed_mb = mb_dl / max(time.time() - _progress._start, 0.1)
            print(f"\r  {bar_fill}{bar_empty}  {c('bold', f'{pct:3d}%')}"
                  f"  {DIM(f'{mb_dl:.1f}/{mb_total:.1f} MB')}"
                  f"  {c('cyan', f'{speed_mb:.1f} MB/s')}",
                  end="", flush=True)

        _progress._start = time.time()
        urllib.request.urlretrieve(url, dest, _progress)
        spinner.stop("")
        print(f"\r  {c('green', '█' * 36)}  {c('bold', '100%')}"
              f"  {c('green', ICONS['ok'] + ' Concluido')}" + " " * 20)
        return True
    except Exception as e:
        spinner.stop("")
        print(f"\n  {ICONS['err']}  {ERR(f'ERRO ao baixar: {e}')}")
        return False

# ---------------------------------------------------------------------------
# Parsing de versoes
# ---------------------------------------------------------------------------

def _semver_key(version_str: str) -> tuple:
    nums = re.findall(r'\d+', version_str)
    return tuple(int(n) for n in nums)

def _parse_docker_versions(html: str) -> list[dict]:
    versions: list[dict] = []
    for m in re.finditer(
        r'<a\s[^>]*href="(docker-([\d.]+(?:-ce)?)\.zip)"[^>]*>.*?(\d+)\s*(MB|KB|GB)?',
        html, re.IGNORECASE
    ):
        filename = m.group(1)
        ver_str  = m.group(2)
        size_num = int(m.group(3))
        size_unit = (m.group(4) or "MB").upper()
        size_mb = size_num
        if size_unit == "GB":
            size_mb = size_num * 1024
        elif size_unit == "KB":
            size_mb = size_num / 1024
        key = _semver_key(ver_str)
        versions.append({
            "version": ver_str, "filename": filename,
            "size_mb": size_mb, "sort_key": key,
            "is_ce": "-ce" in ver_str,
        })
    seen: set[str] = set()
    unique: list[dict] = []
    for v in reversed(sorted(versions, key=lambda x: x["sort_key"])):
        if v["version"] not in seen:
            seen.add(v["version"])
            unique.append(v)
    unique.sort(key=lambda x: x["sort_key"])
    return unique

# ---------------------------------------------------------------------------
# Grid de colunas
# ---------------------------------------------------------------------------

def _print_columns(items: list[tuple[int, str]], cols: int = 5, cell_width: int = 28) -> None:
    rows = (len(items) + cols - 1) // cols
    for row in range(rows):
        parts: list[str] = []
        for col in range(cols):
            idx = col * rows + row
            if idx < len(items):
                num, text = items[idx]
                num_str = c("cyan", f"[{num:2d}]")
                parts.append(f"  {num_str}  {text}".ljust(cell_width + 11))
            else:
                parts.append(" " * (cell_width + 11))
        print("".join(parts))

# ---------------------------------------------------------------------------
# Construcao de opcoes
# ---------------------------------------------------------------------------

def _build_version_options(html: str) -> list[dict]:
    all_versions = _parse_docker_versions(html)
    ce_versions = [v for v in all_versions if v["is_ce"]]
    stable = [v for v in all_versions if not v["is_ce"]]
    groups: dict[int, list[dict]] = defaultdict(list)
    for v in stable:
        groups[v["sort_key"][0]].append(v)
    options: list[dict] = []
    latest = stable[-1] if stable else None
    if latest:
        options.append({
            "label": f"{latest['version']}  ({latest['size_mb']:.0f} MB)",
            "version": latest["version"],
            "tag": "latest",
        })
    options.append(None)
    for major in sorted(groups.keys(), reverse=True):
        recent = groups[major][-8:]
        for v in reversed(recent):
            options.append({
                "label": f"{v['version']}  ({v['size_mb']:.0f} MB)",
                "version": v["version"],
            })
    if ce_versions:
        options.append(None)
        for v in reversed(ce_versions):
            options.append({
                "label": f"{v['version']} [CE]  ({v['size_mb']:.0f} MB)",
                "version": v["version"],
            })
    return options

# ---------------------------------------------------------------------------
# Menus interativos
# ---------------------------------------------------------------------------

def _show_menu_bar() -> None:
    """Barra de atalhos do menu."""
    print(f"  {c('cyan', '[A]')} Atualizar  {c('yellow', '[S]')} Sair  {c('green', '[0]')} Mais recente\n")
    print(f"  {DIM('─' * 64)}")

def select_docker_version() -> str | None:
    clear_screen()
    _print_header(f"{ICONS['box']}  DOCKER ENGINE  {ICONS['box']}")
    spinner = Spinner()
    spinner.start("Buscando versoes disponiveis...")
    try:
        with urllib.request.urlopen(DOCKER_STATIC_BASE) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        spinner.stop(f"  {ICONS['ok']}  {OK('Lista de versoes carregada com sucesso!')}")
    except Exception as e:
        spinner.stop("")
        print(f"\n  {ICONS['err']}  {ERR(f'ERRO ao acessar: {e}')}")
        input(f"\n  Pressione ENTER para sair...")
        return None

    options = _build_version_options(html)
    total = sum(1 for o in options if o is not None)
    print(f"  {ICONS['info']}  {INFO(f'{total} versoes disponiveis')}  |  Fonte: {DIM('download.docker.com')}\n")

    while True:
        clear_screen()
        _print_header(f"{ICONS['box']}  DOCKER ENGINE - Escolha a Versao")
        print(f"  {c('dim', 'Digite o numero da versao desejada:')}\n")

        latest_item: dict | None = None
        grid_opts: list[dict] = []
        for opt in options:
            if opt is None:
                continue
            if opt.get("tag") == "latest":
                latest_item = opt
            else:
                grid_opts.append(opt)

        if latest_item:
            idx_str = c("green", "[ 0]")
            rec_str = c("green", "★ MAIS RECENTE ★")
            print(f"  {idx_str}  {c('bold', latest_item['label'])}     {rec_str}\n")

        grid_items: list[tuple[int, str]] = []
        numbered: list[str] = []
        if latest_item:
            numbered.append(str(latest_item["version"]))
        for i, opt in enumerate(grid_opts):
            idx = len(numbered)
            grid_items.append((idx, opt["label"]))
            numbered.append(str(opt["version"]))

        _print_columns(grid_items, cols=5, cell_width=28)
        print()
        _show_menu_bar()

        choice = input(f"  {c('bold', '>')} ").strip()

        if choice.upper() == "S":
            return None
        if choice.upper() == "A":
            spinner.start("Atualizando lista...")
            try:
                with urllib.request.urlopen(DOCKER_STATIC_BASE) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                options = _build_version_options(html)
                total = sum(1 for o in options if o is not None)
                spinner.stop(f"  {ICONS['ok']}  {OK(f'Lista atualizada! {total} versoes disponiveis.')}")
                input("  Pressione ENTER para continuar...")
            except Exception as e:
                spinner.stop("")
                print(f"\n  {ICONS['err']}  {ERR(str(e))}")
                input("  Pressione ENTER para continuar...")
            continue
        try:
            idx = int(choice)
            if 0 <= idx < len(numbered):
                return numbered[idx]
        except ValueError:
            pass
        q = '"'
        print(f"\n  {ICONS['warn']}  {WARN(f'Opcao invalida: {q}{choice}{q}')}")
        input("  Pressione ENTER para tentar novamente...")

# ---------------------------------------------------------------------------
# GitHub releases
# ---------------------------------------------------------------------------

def _fetch_github_releases(api_url: str, pages: int = 3) -> list[dict]:
    releases: list[dict] = []
    for page in range(1, pages + 1):
        try:
            req = urllib.request.Request(
                f"{api_url}?per_page=30&page={page}",
                headers={"User-Agent": "docker-installer"}
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not data:
                    break
                for rel in data:
                    releases.append({
                        "tag_name": rel["tag_name"],
                        "assets": [
                            {"name": a["name"], "browser_download_url": a["browser_download_url"]}
                            for a in rel.get("assets", [])
                        ],
                        "prerelease": rel.get("prerelease", False),
                    })
        except Exception:
            break
    return releases

def select_github_version(api_url: str, asset_pattern: str, label: str) -> tuple[str | None, str | None]:
    icon = ICONS["gear"]
    clear_screen()
    _print_header(f"{icon}  {label}")
    spinner = Spinner()
    spinner.start("Buscando releases no GitHub...")

    releases = _fetch_github_releases(api_url)
    if not releases:
        spinner.stop("")
        print(f"\n  {ICONS['err']}  {ERR(f'Nao foi possivel obter releases de {label}.')}")
        input("\n  Pressione ENTER para continuar...")
        return None, None

    compatible: list[dict] = []
    for rel in releases:
        for a in rel["assets"]:
            if re.search(asset_pattern, a["name"]):
                compatible.append({
                    "tag": rel["tag_name"],
                    "asset_name": a["name"],
                    "url": a["browser_download_url"],
                    "prerelease": rel["prerelease"],
                })
                break

    spinner.stop(f"  {ICONS['ok']}  {OK(f'{len(compatible)} releases compativeis encontradas')}")
    if not compatible:
        print(f"\n  {ICONS['warn']}  {WARN(f'Nenhum asset compativel para {label}.')}")
        input("\n  Pressione ENTER para continuar...")
        return None, None

    while True:
        clear_screen()
        _print_header(f"{icon}  {label} - Escolha a Versao")
        print(f"  {c('dim', 'Digite o numero da versao desejada:')}\n")

        if compatible:
            c0 = compatible[0]
            pre0 = c("yellow", " [pre-release]") if c0["prerelease"] else ""
            idx_str = c("green", "[ 0]")
            rec_str = c("green", "★ MAIS RECENTE ★")
            print(f"  {idx_str}  {c('bold', c0['tag'])}{pre0}     {rec_str}\n")

        grid_items: list[tuple[int, str]] = []
        for i in range(1, len(compatible)):
            c_item = compatible[i]
            pre = c("yellow", " [pre]") if c_item["prerelease"] else ""
            grid_items.append((i, f"{c_item['tag']}{pre}"))

        _print_columns(grid_items, cols=5, cell_width=24)
        print()
        _show_menu_bar()

        choice = input(f"  {c('bold', '>')} ").strip()

        if choice.upper() == "S":
            return None, None
        if choice.upper() == "A":
            spinner.start("Atualizando lista...")
            releases = _fetch_github_releases(api_url)
            compatible = []
            for rel in releases:
                for a in rel["assets"]:
                    if re.search(asset_pattern, a["name"]):
                        compatible.append({
                            "tag": rel["tag_name"], "asset_name": a["name"],
                            "url": a["browser_download_url"],
                            "prerelease": rel["prerelease"],
                        })
                        break
            spinner.stop(f"  {ICONS['ok']}  {OK(f'{len(compatible)} releases encontradas')}")
            if not compatible:
                print(f"\n  {ICONS['warn']}  Nenhum asset compativel.")
                input("  Pressione ENTER...")
                return None, None
            input("  Pressione ENTER para continuar...")
            continue
        try:
            idx = int(choice)
            if 0 <= idx < len(compatible):
                sel = compatible[idx]
                return sel["url"], sel["tag"]
        except ValueError:
            pass
        q = '"'
        print(f"\n  {ICONS['warn']}  {WARN(f'Opcao invalida: {q}{choice}{q}')}")
        input("  Pressione ENTER para tentar novamente...")

# ---------------------------------------------------------------------------
# Download dos binarios
# ---------------------------------------------------------------------------

def download_docker_engine(dest_dir: str, version: str) -> bool:
    url = f"{DOCKER_STATIC_BASE}docker-{version}.zip"
    tmp_zip = os.path.join(tempfile.gettempdir(), f"docker-{version}.zip")
    if not download_file(url, tmp_zip, f"Docker Engine {c('bold', version)}"):
        return False
    print(f"\n  {ICONS['disk']}  Extraindo para {c('cyan', dest_dir)}...")
    spinner = Spinner()
    try:
        ensure_dir(dest_dir)
        count = 0
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            for member in zf.namelist():
                if member.endswith(".exe") and member.startswith("docker/") and "/" not in member[7:]:
                    fname = os.path.basename(member)
                    dest_path = os.path.join(dest_dir, fname)
                    spinner.start(f"Extraindo: {c('bold', fname)}")
                    with zf.open(member) as src, open(dest_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    spinner.stop(f"  {ICONS['ok']}  {OK(fname)}")
                    count += 1
        print(f"\n  {ICONS['ok']}  {OK(f'{count} arquivos extraidos com sucesso')}")
        return True
    except Exception as e:
        spinner.stop("")
        print(f"\n  {ICONS['err']}  {ERR(f'ERRO ao extrair: {e}')}")
        return False
    finally:
        try:
            os.remove(tmp_zip)
        except OSError:
            pass

def download_github_asset_direct(download_url: str, dest_path: str, label: str) -> bool:
    ensure_dir(os.path.dirname(dest_path))
    return download_file(download_url, dest_path, label)

# ---------------------------------------------------------------------------
# IntelliJ
# ---------------------------------------------------------------------------

def find_intellij_directory(base_path: str) -> str | None:
    pattern = os.path.join(base_path, "JetBrains", "IntelliJIdea*")
    directories = glob.glob(pattern)
    if directories:
        return max(directories, key=os.path.getctime)
    return None

def install_intellij_configs(script_dir: str) -> None:
    appdata = os.getenv("APPDATA", "")
    intellij_dir = find_intellij_directory(appdata)
    if not intellij_dir:
        print(f"\n  {ICONS['warn']}  {WARN('IntelliJIdea nao encontrado.')}")
        print(f"  {DIM('As configs deverao ser copiadas manualmente.')}")
        return
    options_dir = os.path.join(intellij_dir, "options")
    ensure_dir(options_dir)
    for fname in ("docker-tools.xml", "remote-servers.xml"):
        src = os.path.join(script_dir, fname)
        dst = os.path.join(options_dir, fname)
        if os.path.exists(src):
            shutil.copy(src, dst)
            print(f"  {ICONS['ok']}  {fname} {DIM('→')} {c('cyan', dst)}")
        else:
            print(f"  {ICONS['warn']}  {WARN(f'{src} nao encontrado')}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run_installer() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    docker_cli_dir = r"C:\Docker-CLI"
    docker_plugins_dir = os.path.expanduser(r"~\.docker\cli-plugins")

    # ── 1. DOCKER ENGINE ──
    _step_header(1, "DOCKER ENGINE")
    engine_version = select_docker_version()
    if engine_version is None:
        print(f"\n  {ICONS['warn']}  {WARN('Instalacao cancelada pelo usuario.')}")
        return
    clear_screen()
    _print_header(f"{ICONS['box']}  INSTALANDO DOCKER ENGINE")
    print(f"  {ICONS['info']}  Versao selecionada: {c('bold', engine_version)}")
    print(f"  {ICONS['info']}  Destino: {c('cyan', docker_cli_dir)}")
    if not download_docker_engine(docker_cli_dir, engine_version):
        print(f"\n  {ICONS['err']}  {ERR('FALHA ao instalar Docker Engine.')}")
        input("  Pressione ENTER para sair...")
        return
    print(f"\n  {ICONS['ok']}  {OK(f'Docker Engine {engine_version} instalado!')}")
    input(f"\n  Pressione ENTER para continuar...")

    # ── 2. DOCKER COMPOSE ──
    _step_header(2, "DOCKER COMPOSE")
    compose_url, compose_tag = select_github_version(
        DOCKER_COMPOSE_API,
        r"docker-compose-windows-x86_64\.exe$",
        "DOCKER COMPOSE"
    )
    if compose_url:
        clear_screen()
        _print_header(f"{ICONS['gear']}  INSTALANDO DOCKER COMPOSE")
        print(f"  {ICONS['info']}  Versao: {c('bold', compose_tag)}")
        compose_dest = os.path.join(docker_cli_dir, "docker-compose.exe")
        ok = download_github_asset_direct(compose_url, compose_dest, f"Docker Compose {compose_tag}")
        if ok:
            print(f"\n  {ICONS['ok']}  {OK(f'Docker Compose {compose_tag} instalado!')}")
        else:
            print(f"\n  {ICONS['warn']}  {WARN('Falha ao baixar Docker Compose.')}")
        input(f"\n  Pressione ENTER para continuar...")
    else:
        print(f"\n  {DIM('Docker Compose: pulado pelo usuario.')}")

    # ── 3. DOCKER BUILDX ──
    _step_header(3, "DOCKER BUILDX")
    buildx_url, buildx_tag = select_github_version(
        DOCKER_BUILDX_API,
        r"buildx-v[\d.]+\.windows-amd64\.exe$",
        "DOCKER BUILDX"
    )
    if buildx_url:
        clear_screen()
        _print_header(f"{ICONS['gear']}  INSTALANDO DOCKER BUILDX")
        print(f"  {ICONS['info']}  Versao: {c('bold', buildx_tag)}")
        buildx_dest = os.path.join(docker_plugins_dir, "docker-buildx.exe")
        ok = download_github_asset_direct(buildx_url, buildx_dest, f"Docker Buildx {buildx_tag}")
        if ok:
            print(f"\n  {ICONS['ok']}  {OK(f'Docker Buildx {buildx_tag} instalado!')}")
        else:
            print(f"\n  {ICONS['warn']}  {WARN('Falha ao baixar Docker Buildx.')}")
        input(f"\n  Pressione ENTER para continuar...")
    else:
        print(f"\n  {DIM('Docker Buildx: pulado pelo usuario.')}")

    # ── 4. INTELLIJ ──
    clear_screen()
    _print_header(f"{ICONS['disk']}  CONFIGURANDO INTELLIJ IDEA", "blue")
    install_intellij_configs(script_dir)

    # ── RESUMO ──
    clear_screen()
    _print_header(f"{ICONS['ship']}  INSTALACAO CONCLUIDA", "green")
    print(f"  {c('bold', 'Resumo da instalacao:')}\n")
    _summary_line("Docker CLI", docker_cli_dir, ICONS["ok"])
    _summary_line("Buildx plugin", docker_plugins_dir, ICONS["ok"])
    _summary_line("IntelliJ", "npipe:////./pipe/docker_engine", ICONS["ok"])
    print(f"\n  {c('dim', '─' * 60)}")
    print(f"  {c('bold', 'Verifique com:')}")
    print(f"    {c('cyan', 'docker --version')}")
    print(f"    {c('cyan', 'docker compose version')}")
    print(f"    {c('cyan', 'docker buildx version')}")
    print()
    input(f"  {c('bold', 'Pressione ENTER para sair...')}")

def main() -> None:
    maximize_console()
    if not is_admin():
        import subprocess as _sp
        args = _sp.list2cmdline([sys.executable] + sys.argv)
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, args, None, 1
        )
        sys.exit(0)
    try:
        _run_installer()
    except Exception:
        print("\n" + c("red", "╔" + "═" * 58 + "╗"))
        print(c("red", "║") + c("bold", "  ERRO INESPERADO").center(58) + c("red", "║"))
        print(c("red", "╚" + "═" * 58 + "╝"))
        traceback.print_exc()
        input(f"\n  Pressione ENTER para sair...")

if __name__ == "__main__":
    main()
