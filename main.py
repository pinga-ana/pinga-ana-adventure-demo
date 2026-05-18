import array
import asyncio
import base64
import io
import json
import math
import random
import struct
import sys
from pathlib import Path
from urllib.parse import urljoin

import pygame


def _compute_runs_in_browser_wasm() -> bool:
    """Browser/pygbag: não usar import platform.machine() — pygame já carrega o stdlib platform
    e quebra `from platform import window` da documentação pygame-web."""
    if sys.platform in ("emscripten", "wasi"):
        return True
    try:
        __import__("js")
        return True
    except ImportError:
        pass
    for mod in sys.modules:
        if mod.startswith("pyodide"):
            return True
    return False


pygame.init()

_RUNS_IN_BROWSER_WASM = _compute_runs_in_browser_wasm()

# No WASM não usamos pygame.mixer.Sound (OOB no Chrome). Som: Web Audio + fallback HTMLAudioElement (data URL).

_WASM_WEB_AC: object | None = None
_WASM_WEB_BG: tuple[object, object] | None = None
# Um único <audio> para música em loop (seleção + jogo): iOS rejeita novo .play()
# após removeChild/teardown fora da cadeia de gesto inicial.
_WASM_HTML5_LOOP_EL: object | None = None

# pygbag: POST via `aio.fetch.RequestHandler` (não existe `pyodide.http.pyfetch` no stub).
_wasm_aio_fetch_handler: object | None = None

DEFAULT_SOM: dict[str, float] = {
    "musica_selecao_pygame": 0.32,
    "musica_selecao_wasm": 0.22,
    "musica_jogo_pygame": 0.58,
    "musica_jogo_ficheiro_wasm": 0.52,
    "musica_jogo_loop_sintetico_wasm": 0.38,
    "musica_jogo_oscilador_wasm": 0.05,
    "loop_procedural_pygame": 0.36,
    "sfx_acerto_pygame": 0.48,
    "sfx_dano_pygame": 0.58,
    "sfx_acerto_wasm": 0.55,
    "sfx_dano_wasm": 0.58,
    "desbloqueio_audio_wasm": 0.08,
}

_AUDIO_VOL: dict[str, float] = dict(DEFAULT_SOM)


def _merge_som_config(base: dict[str, float], patch: object) -> dict[str, float]:
    out = dict(base)
    if not isinstance(patch, dict):
        return out
    for k, v in patch.items():
        if k not in DEFAULT_SOM:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        out[k] = max(0.0, min(1.0, f))
    return out


def set_audio_vol_from_cfg(cfg: dict) -> None:
    global _AUDIO_VOL
    raw = cfg.get("som")
    if isinstance(raw, dict):
        _AUDIO_VOL = _merge_som_config(dict(DEFAULT_SOM), raw)
    else:
        _AUDIO_VOL = dict(DEFAULT_SOM)


def _audio_vol(key: str) -> float:
    try:
        return max(0.0, min(1.0, float(_AUDIO_VOL.get(key, DEFAULT_SOM.get(key, 0.5)))))
    except (TypeError, ValueError):
        return 0.5


def _audio_vol_osc_wasm(key: str) -> float:
    """Ganho do oscilador de fallback (evita valores demasiado altos)."""
    try:
        return max(0.0, min(0.2, float(_AUDIO_VOL.get(key, DEFAULT_SOM.get(key, 0.05)))))
    except (TypeError, ValueError):
        return 0.05


def _wasm_tiny_wav(freq_hz: float, duration_ms: int, sample_rate: int = 11025) -> bytes:
    """WAV curto para data:audio/wav;base64 (HTML5 Audio no Chrome mobile)."""
    n = max(1, int(sample_rate * duration_ms / 1000))
    out = array.array("h")
    for i in range(n):
        t = i / sample_rate
        a = min(1.0, i / max(1, int(n * 0.08)))
        b = min(1.0, (n - 1 - i) / max(1, int(n * 0.25)))
        env = a * b
        v = math.sin(2 * math.pi * freq_hz * t) * env
        out.append(int(max(-1.0, min(1.0, v)) * 32000))
    return _mono16_pcm_to_wav(out, sample_rate)


def _wasm_soft_loop_wav(sample_rate: int = 8000, duration_s: float = 1.2) -> bytes:
    """Loop curto e leve para música de fundo em base64."""
    n = max(1, int(sample_rate * duration_s))
    out = array.array("h")
    for i in range(n):
        t = i / sample_rate
        s = 0.22 * math.sin(2 * math.pi * 65.0 * t)
        s += 0.14 * math.sin(2 * math.pi * 98.0 * t + 0.3)
        s *= 0.55 + 0.45 * math.sin(2 * math.pi * 0.35 * t)
        env = min(1.0, i / max(1, int(n * 0.05))) * min(1.0, (n - 1 - i) / max(1, int(n * 0.05)))
        out.append(int(max(-1.0, min(1.0, s * env)) * 12000))
    return _mono16_pcm_to_wav(out, sample_rate)


def _wasm_js_audio_ctor() -> object | None:
    try:
        from js import Audio  # type: ignore[import-not-found]

        return Audio
    except Exception:
        pass
    try:
        j = __import__("js")
        return getattr(j, "Audio", None)
    except Exception:
        return None


def _wasm_html5_audio_attach(el: object, *, append_to_dom: bool) -> None:
    """playsinline ajuda iOS; append ao body só para loop (um nó) — evita fugas nos SFX."""
    try:
        el.setAttribute("playsinline", "")
        el.setAttribute("webkit-playsinline", "")
    except Exception:
        pass
    if not append_to_dom:
        return
    try:
        from js import document  # type: ignore[import-not-found]

        b = document.body
        if b is not None and hasattr(b, "appendChild"):
            b.appendChild(el)
    except Exception:
        pass


def _wasm_audio_mime_for_path(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".oga": "audio/ogg",
        ".aac": "audio/aac",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".mid": "audio/midi",
        ".midi": "audio/midi",
    }.get(ext, "application/octet-stream")


def _wasm_bgm_path_playable_html5(pack_path: Path | None) -> Path | None:
    """MIDI via `<audio src=data:audio/midi>` é mal suportado (iOS/Android: mudo ou erro).
    Ficheiros comprimidos tocam; para .mid tenta homólogo (mesmo nome), sufixos *_wasm/_web e extensões."""
    if pack_path is None or not pack_path.is_file():
        return None
    suf = pack_path.suffix.lower()
    if suf not in (".mid", ".midi"):
        return pack_path
    stem = pack_path.stem
    parent = pack_path.parent
    for tag in ("_wasm", "_web", "_mobile"):
        for ext in (".ogg", ".opus", ".aac", ".m4a", ".mp3", ".wav"):
            alt = parent / f"{stem}{tag}{ext}"
            if alt.is_file():
                return alt
    for ext in (".ogg", ".opus", ".aac", ".m4a", ".mp3", ".wav"):
        alt = pack_path.with_suffix(ext)
        if alt.is_file():
            return alt
    return None


def _wasm_audio_data_url(path: Path) -> str | None:
    """Lê áudio do FS embebido (pós-extract do .tar.gz) e expõe como data URL.

    No GitHub Pages o pacote pygbag não publica `assets/sounds/` como ficheiros HTTP;
    `assets/...` no `<audio>` falha com 404. O Python vê os ficheiros em disco após extract.
    """
    if not _RUNS_IN_BROWSER_WASM:
        return None
    max_raw = 6 * 1024 * 1024
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > max_raw:
        return None
    mime = _wasm_audio_mime_for_path(path)
    try:
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return None


def _wasm_html5_audio_hard_stop(el: object | None) -> None:
    """Para de vez um <audio> (loop + data URL podem continuar só com pause)."""
    if el is None:
        return
    try:
        el.loop = False
        el.pause()
        el.currentTime = 0
    except Exception:
        pass
    try:
        el.src = ""
    except Exception:
        try:
            el.removeAttribute("src")
        except Exception:
            pass
    try:
        el.load()
    except Exception:
        pass
    try:
        from js import document  # type: ignore[import-not-found]

        b = document.body
        if b is not None and getattr(el, "parentNode", None) is b:
            b.removeChild(el)
    except Exception:
        pass


def _wasm_web_audio_oscillator_stop_only() -> None:
    """Para só o oscilador de BGM fallback (o <audio> em loop trata-se à parte)."""
    global _WASM_WEB_BG
    if _WASM_WEB_BG is None:
        return
    try:
        osc, _gn = _WASM_WEB_BG
        ctx = _wasm_web_audio_context()
        if ctx is not None and hasattr(osc, "stop"):
            osc.stop(float(ctx.currentTime))
    except Exception:
        pass
    _WASM_WEB_BG = None


def _wasm_loop_bgm_play(url: str, *, volume: float, loop: bool = True) -> bool:
    """Um único `<audio>` em loop: troca `src` sem removeChild (Safari/iOS bloqueia novo nó)."""
    global _WASM_HTML5_LOOP_EL
    if not _RUNS_IN_BROWSER_WASM or not url or not isinstance(url, str):
        return False
    el = _WASM_HTML5_LOOP_EL
    if el is None:
        try:
            from js import document  # type: ignore[import-not-found]

            if hasattr(document, "createElement"):
                el = document.createElement("audio")
                el.preload = "auto"
        except Exception:
            el = None
        if el is None:
            Ctor = _wasm_js_audio_ctor()
            if Ctor is None:
                return False
            try:
                try:
                    el = Ctor.new("")
                except Exception:
                    el = Ctor("")
            except Exception:
                return False
        _wasm_html5_audio_attach(el, append_to_dom=True)
        _WASM_HTML5_LOOP_EL = el
    el = _WASM_HTML5_LOOP_EL
    assert el is not None
    try:
        try:
            el.pause()
        except Exception:
            pass
        el.volume = float(volume)
        el.loop = bool(loop)
        el.src = url
        try:
            el.load()
        except Exception:
            pass
        play_ret = el.play()
        if play_ret is not None and hasattr(play_ret, "catch"):
            play_ret.catch(lambda *_: None)
        return True
    except Exception:
        return False


def _wasm_loop_bgm_pause_only() -> None:
    el = _WASM_HTML5_LOOP_EL
    if el is None:
        return
    try:
        el.pause()
    except Exception:
        pass


def _wasm_loop_bgm_resume() -> bool:
    """Retoma após mute por UI; só se `src` ainda estiver definido."""
    el = _WASM_HTML5_LOOP_EL
    if el is None:
        return False
    try:
        src = str(getattr(el, "src", "") or getattr(el, "currentSrc", "") or "").strip()
        if not src:
            return False
        play_ret = el.play()
        if play_ret is not None and hasattr(play_ret, "catch"):
            play_ret.catch(lambda *_: None)
        return True
    except Exception:
        return False


def _wasm_loop_bgm_soft_clear() -> None:
    """Para e limpa `src` sem remover o nó do DOM (reutilização iOS)."""
    el = _WASM_HTML5_LOOP_EL
    if el is None:
        return
    try:
        el.loop = False
        el.pause()
        el.currentTime = 0
    except Exception:
        pass
    try:
        el.src = ""
    except Exception:
        try:
            el.removeAttribute("src")
        except Exception:
            pass
    try:
        el.load()
    except Exception:
        pass


def _wasm_selection_music_start(path: Path | None) -> None:
    if not _RUNS_IN_BROWSER_WASM or path is None:
        return
    _wasm_web_audio_oscillator_stop_only()
    vol = _audio_vol("musica_selecao_wasm")
    candidates: list[Path] = [path]
    if path.suffix.lower() == ".aac":
        alt = path.with_suffix(".ogg")
        if alt.is_file() and alt.resolve() != path.resolve():
            candidates.append(alt)
    for cand in candidates:
        if not cand.is_file():
            continue
        url = _wasm_audio_data_url(cand)
        if url and _wasm_loop_bgm_play(url, volume=vol, loop=True):
            return


def _wasm_play_wav_html5(
    wav: bytes, *, volume: float = 0.5, loop: bool = False, append_to_dom: bool | None = None
) -> object | None:
    """Reproduz WAV via <audio> + data URL (funciona bem no Chrome Android)."""
    if not _RUNS_IN_BROWSER_WASM:
        return None
    if append_to_dom is None:
        append_to_dom = bool(loop)
    url = "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
    el = None
    try:
        from js import document  # type: ignore[import-not-found]

        if hasattr(document, "createElement"):
            el = document.createElement("audio")
            el.preload = "auto"
            el.src = url
    except Exception:
        el = None
    if el is None:
        Ctor = _wasm_js_audio_ctor()
        if Ctor is None:
            return None
        try:
            try:
                el = Ctor.new(url)
            except Exception:
                el = Ctor(url)
        except Exception:
            return None
    try:
        _wasm_html5_audio_attach(el, append_to_dom=append_to_dom)
        el.volume = float(volume)
        el.loop = bool(loop)
        play_ret = el.play()
        if play_ret is not None and hasattr(play_ret, "catch"):
            play_ret.catch(lambda *_: None)
        return el
    except Exception:
        return None


def _wasm_web_audio_context() -> object | None:
    global _WASM_WEB_AC
    if not _RUNS_IN_BROWSER_WASM:
        return None
    if _WASM_WEB_AC is not None:
        return _WASM_WEB_AC
    try:
        from js import AudioContext  # type: ignore[import-not-found]

        _WASM_WEB_AC = AudioContext.new()
        return _WASM_WEB_AC
    except Exception:
        pass
    try:
        j = __import__("js")
        AC = getattr(j, "AudioContext", None) or getattr(j, "webkitAudioContext", None)
        if AC is not None:
            _WASM_WEB_AC = AC.new()
            return _WASM_WEB_AC
    except Exception:
        pass
    return None


def _wasm_web_audio_resume(ctx: object | None) -> None:
    if ctx is None:
        return
    try:
        if getattr(ctx, "state", "") == "suspended" and hasattr(ctx, "resume"):
            r = ctx.resume()
            if r is not None and hasattr(r, "catch"):
                r.catch(lambda *_: None)
    except Exception:
        pass


def _wasm_web_audio_tone(
    freq: float,
    dur: float,
    vol: float,
    shape: str = "sine",
    delay: float = 0.0,
) -> None:
    ctx = _wasm_web_audio_context()
    if ctx is None:
        return
    _wasm_web_audio_resume(ctx)
    try:
        t0 = float(ctx.currentTime) + float(delay)
        osc = ctx.createOscillator()
        gn = ctx.createGain()
        osc.type = shape
        osc.frequency.value = float(freq)
        gn.gain.value = float(vol)
        osc.connect(gn)
        gn.connect(ctx.destination)
        osc.start(t0)
        osc.stop(t0 + float(dur))
    except Exception:
        pass


def _wasm_web_audio_hit() -> None:
    el = _wasm_play_wav_html5(
        _wasm_tiny_wav(720.0, 55, 11025), volume=_audio_vol("sfx_acerto_wasm"), loop=False
    )
    if el is None:
        _wasm_web_audio_tone(340, 0.04, 0.11, "square", 0.0)
        _wasm_web_audio_tone(190, 0.055, 0.08, "sine", 0.02)


def _wasm_web_audio_hurt() -> None:
    el = _wasm_play_wav_html5(
        _wasm_tiny_wav(95.0, 200, 11025), volume=_audio_vol("sfx_dano_wasm"), loop=False
    )
    if el is None:
        _wasm_web_audio_tone(88, 0.24, 0.13, "triangle", 0.0)


def _wasm_web_audio_bg_start(*, pack_path: Path | None = None) -> None:
    global _WASM_WEB_BG
    if not _RUNS_IN_BROWSER_WASM:
        return
    _wasm_web_audio_oscillator_stop_only()
    html5_bgm = _wasm_bgm_path_playable_html5(pack_path)
    if html5_bgm is not None:
        u = _wasm_audio_data_url(html5_bgm)
        if u and _wasm_loop_bgm_play(u, volume=_audio_vol("musica_jogo_ficheiro_wasm"), loop=True):
            return
    wav_url = "data:audio/wav;base64," + base64.b64encode(_wasm_soft_loop_wav()).decode("ascii")
    if _wasm_loop_bgm_play(wav_url, volume=_audio_vol("musica_jogo_loop_sintetico_wasm"), loop=True):
        return
    _wasm_loop_bgm_soft_clear()
    ctx = _wasm_web_audio_context()
    if ctx is None:
        return
    _wasm_web_audio_resume(ctx)
    try:
        osc = ctx.createOscillator()
        gn = ctx.createGain()
        osc.type = "sine"
        osc.frequency.value = 72.0
        gn.gain.value = _audio_vol_osc_wasm("musica_jogo_oscilador_wasm")
        osc.connect(gn)
        gn.connect(ctx.destination)
        osc.start(float(ctx.currentTime))
        _WASM_WEB_BG = (osc, gn)
    except Exception:
        _WASM_WEB_BG = None


def _wasm_web_audio_bg_stop() -> None:
    _wasm_loop_bgm_soft_clear()
    _wasm_web_audio_oscillator_stop_only()


def _wasm_web_audio_prime() -> None:
    """Desbloqueia áudio no mesmo stack do toque (Chrome/Android bloqueiam após yield/async)."""
    ctx = _wasm_web_audio_context()
    _wasm_web_audio_resume(ctx)
    _ = _wasm_play_wav_html5(
        _wasm_tiny_wav(220.0, 25, 8000),
        volume=_audio_vol("desbloqueio_audio_wasm"),
        loop=False,
        append_to_dom=True,
    )


def _mono16_pcm_to_wav(samples: array.array, sample_rate: int) -> bytes:
    """PCM mono 16-bit LE → ficheiro WAV em bytes.

    Não usar o stdlib `wave`: no pygbag o PEP 723 tenta `pip install wave` e instala
    um pacote PyPI incompatível, quebrando o arranque no browser.
    """
    pcm = samples.tobytes()
    n = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + n,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        n,
    )
    return header + pcm


def _synth_hit_enemy(sr: int = 22050) -> bytes:
    """Impacto curto: ruído + grave em decaimento (acerto no inimigo)."""
    dur = 0.075
    n = max(1, int(sr * dur))
    out = array.array("h")
    for i in range(n):
        t = i / sr
        env = math.exp(-18.0 * i / n)
        f = 280.0 * math.exp(-9.0 * t)
        thump = 0.5 * math.sin(2 * math.pi * f * t)
        noise = (random.random() * 2.0 - 1.0) * 0.38
        s = (thump + noise) * env
        out.append(int(max(-1.0, min(1.0, s)) * 30000))
    return _mono16_pcm_to_wav(out, sr)


def _synth_hurt_player(sr: int = 22050) -> bytes:
    """Tom mais baixo que desce (levou dano)."""
    dur = 0.2
    n = max(1, int(sr * dur))
    out = array.array("h")
    for i in range(n):
        t = i / sr
        f = 175.0 - 105.0 * (i / n)
        env = math.sin(math.pi * i / n) ** 1.4
        s = 0.72 * math.sin(2 * math.pi * f * t) * env
        out.append(int(max(-1.0, min(1.0, s)) * 30500))
    return _mono16_pcm_to_wav(out, sr)


def _synth_bg_loop(sr: int = 22050) -> bytes:
    """Loop ambiente suave (acordes graves + tremolo)."""
    dur = 2.56
    n = max(1, int(sr * dur))
    freqs = (98.0, 130.81, 164.81, 196.0)
    out = array.array("h")
    edge = max(1, int(sr * 0.05))
    for i in range(n):
        t = i / sr
        trem = 0.82 + 0.18 * math.sin(2 * math.pi * 0.42 * t)
        s = 0.0
        for k, f in enumerate(freqs):
            s += (0.2 / len(freqs)) * math.sin(2 * math.pi * f * t + 0.35 * k)
        s *= trem
        fade = 1.0
        if i < edge:
            fade = i / edge
        elif i > n - edge:
            fade = (n - 1 - i) / max(1, edge - 1)
        s *= fade
        out.append(int(max(-1.0, min(1.0, s * 0.5)) * 26000))
    return _mono16_pcm_to_wav(out, sr)


def load_procedural_sounds() -> tuple[pygame.mixer.Sound | None, pygame.mixer.Sound | None, pygame.mixer.Sound | None]:
    """Sons SDL (desktop). No browser (WASM) usar `_wasm_web_audio_*` — mixer+Sound corrompem o heap no Chrome."""
    if _RUNS_IN_BROWSER_WASM:
        return None, None, None
    sr = 22050
    try:
        if pygame.mixer.get_init() is None:
            pygame.mixer.init(frequency=sr, size=-16, channels=2, buffer=1024)
        pygame.mixer.set_num_channels(16)
        hit = pygame.mixer.Sound(io.BytesIO(_synth_hit_enemy(sr)))
        hurt = pygame.mixer.Sound(io.BytesIO(_synth_hurt_player(sr)))
        bg = pygame.mixer.Sound(io.BytesIO(_synth_bg_loop(sr)))
        hit.set_volume(_audio_vol("sfx_acerto_pygame"))
        hurt.set_volume(_audio_vol("sfx_dano_pygame"))
        bg.set_volume(_audio_vol("loop_procedural_pygame"))
        return hit, hurt, bg
    except (pygame.error, OSError, ValueError, TypeError):
        return None, None, None

WIDTH, HEIGHT = 360, 640
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Pinga Ana Adventure")
clock = pygame.time.Clock()


def _display_px() -> tuple[float, float]:
    surf = pygame.display.get_surface()
    if surf is None:
        return float(WIDTH), float(HEIGHT)
    w, h = surf.get_size()
    return float(w), float(h)


def _finger_event_to_px(event: pygame.event.Event) -> tuple[float, float]:
    """Pygame/SDL: normalmente normalizado em [0,1]; alguns backends (pygbag) enviam pixéis."""
    sx, sy = _display_px()
    xf, yf = float(event.x), float(event.y)
    if xf <= 1.0 + 1e-6 and yf <= 1.0 + 1e-6 and xf >= -1e-6 and yf >= -1e-6:
        return xf * sx, yf * sy
    return xf, yf


def _mouse_click_is_left(event: pygame.event.Event) -> bool:
    """Toque→rato sintetizado pode usar botão 0 em vez de BUTTON_LEFT (=1)."""
    return bool(getattr(event, "type", None) == pygame.MOUSEBUTTONDOWN and getattr(event, "button", 1) in (0, 1))


def _mouse_down_pos_px(event: pygame.event.Event) -> tuple[float, float]:
    if hasattr(event, "pos"):
        return float(event.pos[0]), float(event.pos[1])
    mx, my = pygame.mouse.get_pos()
    return float(mx), float(my)


def _mouse_motion_drag_pos_px(event: pygame.event.Event) -> tuple[float, float]:
    return _mouse_down_pos_px(event)


def _present_display() -> None:
    """No browser o canvas SDL costuma actualizar-se melhor com update() que com flip()."""
    if _RUNS_IN_BROWSER_WASM:
        pygame.display.update()
    else:
        pygame.display.flip()


def _first_square_frame(surf: pygame.Surface) -> pygame.Surface:
    """Tiras horizontais (ex. 600x100 com frames 100x100): usa só o 1.º frame quadrado."""
    w, h = surf.get_size()
    if w > h and h >= 16:
        return surf.subsurface((0, 0, h, h)).copy()
    return surf


def _crop_to_opaque_bounds(surf: pygame.Surface) -> pygame.Surface:
    """Remove margens transparentes para o escalonamento preencher o tamanho do sprite (rect = imagem visível)."""
    try:
        m = pygame.mask.from_surface(surf, 127)
    except (ValueError, pygame.error):
        return surf
    rects = m.get_bounding_rects()
    if not rects:
        return surf
    bb = pygame.Rect(rects[0])
    for r in rects[1:]:
        bb.union_ip(r)
    bb = bb.clip(surf.get_rect())
    if bb.width < 2 or bb.height < 2:
        return surf
    return surf.subsurface(bb).copy()


DEFAULT_CHARACTERS: list[dict] = [
    {
        "id": "arthas",
        "sprite": "novos/player_arthas.png",
        "name": "Arthas",
        "title": "O Pirata",
        "forca": 1,
        "resistencia": 1,
        "velocidade": 1,
        "velocidade_tiro": 1,
    },
    {
        "id": "penetrus",
        "sprite": "novos/player_penetrus.png",
        "name": "Penetrus",
        "title": "Mago",
        "forca": 2,
        "resistencia": 1,
        "velocidade": 1,
        "velocidade_tiro": 2,
    },
    {
        "id": "uni_orc",
        "sprite": "novos/player_uni_orc.png",
        "name": "Uni-Orc",
        "title": "Orc Unicórnio",
        "forca": 1,
        "resistencia": 2,
        "velocidade": 1,
        "velocidade_tiro": 1,
    },
    {
        "id": "red_oni",
        "sprite": "novos/player_red_oni.png",
        "name": "Red Oni",
        "title": "Demônio japonês",
        "forca": 2,
        "resistencia": 1,
        "velocidade": 2,
        "velocidade_tiro": 1,
    },
    {
        "id": "sr_baldius",
        "sprite": "novos/player_sr_baldius.png",
        "name": "Sr. Baldius",
        "title": "Soldado Templário",
        "forca": 2,
        "resistencia": 2,
        "velocidade": 1,
        "velocidade_tiro": 1,
    },
]


def _normalize_character(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    cid = raw.get("id")
    name = raw.get("name")
    if not isinstance(cid, str) or not cid.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    title = raw.get("title", "")
    if not isinstance(title, str):
        title = str(title)

    def _num(key: str, default: float = 1.0) -> float:
        v = raw.get(key, default)
        try:
            n = float(v)
        except (TypeError, ValueError):
            return default
        return max(0.1, n)

    cid_key = cid.strip()
    sprite_raw = raw.get("sprite")
    if isinstance(sprite_raw, str) and sprite_raw.strip():
        sprite = sprite_raw.strip()
    else:
        sprite = f"novos/player_{cid_key}.png"

    out: dict = {
        "id": cid_key,
        "sprite": sprite,
        "name": name.strip(),
        "title": title.strip(),
        "forca": _num("forca", 1.0),
        "resistencia": int(max(1, round(_num("resistencia", 1.0)))),
        "velocidade": _num("velocidade", 1.0),
        "velocidade_tiro": _num("velocidade_tiro", 1.0),
    }
    musica_fundo = raw.get("musica_fundo")
    if isinstance(musica_fundo, str) and musica_fundo.strip():
        out["musica_fundo"] = musica_fundo.strip()
    musica_fundo_wasm = raw.get("musica_fundo_wasm")
    if isinstance(musica_fundo_wasm, str) and musica_fundo_wasm.strip():
        out["musica_fundo_wasm"] = musica_fundo_wasm.strip()
    return out


def load_characters_from_config(patch: dict) -> list[dict]:
    raw_list = patch.get("characters")
    if not isinstance(raw_list, list) or not raw_list:
        return [dict(c) for c in DEFAULT_CHARACTERS]
    out: list[dict] = []
    for item in raw_list:
        norm = _normalize_character(item) if isinstance(item, dict) else None
        if norm:
            out.append(norm)
    return out if out else [dict(c) for c in DEFAULT_CHARACTERS]


DEFAULT_ENEMIES_MERGE: dict[str, dict] = {
    "orc": {
        "sprite": "enemy.png",
        "resistencia": 2,
        "velocidade": 1.0,
        "comeca_apos_pontos": 0,
    },
    "soldado": {
        "sprite": "Characters(100x100)/Soldier/Soldier/Soldier-Idle.png",
        "resistencia": 2,
        "velocidade": 0.58,
        "comeca_apos_pontos": 20,
    },
}


def _normalize_enemy_entry(stats: dict, eid: str) -> dict:
    resistencia = stats.get("resistencia", stats.get("hits_to_destroy", 1))
    try:
        resistencia_i = max(1, int(resistencia))
    except (TypeError, ValueError):
        resistencia_i = 1
    sprite = stats.get("sprite", "enemy.png")
    if not isinstance(sprite, str) or not sprite.strip():
        sprite = "enemy.png"
    sprite = sprite.strip()
    try:
        vel = float(stats.get("velocidade", 1.0))
    except (TypeError, ValueError):
        vel = 1.0
    vel = max(0.12, min(4.0, vel))
    ap = stats.get("comeca_apos_pontos", stats.get("comeca_apos_pontuacao", 0))
    try:
        desde = max(0, int(ap))
    except (TypeError, ValueError):
        desde = 0
    return {
        "id": str(eid),
        "sprite": sprite,
        "resistencia": resistencia_i,
        "velocidade": vel,
        "comeca_apos_pontos": desde,
    }


def _merge_enemies_config(base: dict[str, dict], patch: dict | None) -> dict[str, dict]:
    merged = {k: dict(v) for k, v in base.items()}
    if not isinstance(patch, dict):
        return {k: _normalize_enemy_entry(v, k) for k, v in merged.items()}
    for name, stats in patch.items():
        if not isinstance(stats, dict):
            continue
        prev = merged.get(name, {})
        combined = {**prev, **stats}
        merged[name] = _normalize_enemy_entry(combined, str(name))
    return merged if merged else {
        k: _normalize_enemy_entry(v, k) for k, v in DEFAULT_ENEMIES_MERGE.items()
    }


def _load_build_params_dict() -> dict[str, object]:
    """Metadados de build (`build_params.json`); o CI escreve o ficheiro antes do pygbag."""
    base = Path(__file__).resolve().parent
    out: dict[str, object] = {"build_number": 0, "run_id": "", "sha": ""}
    for path in (base / "build_params.json", base / "assets" / "build_params.json"):
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                try:
                    out["build_number"] = int(raw.get("build_number", 0))
                except (TypeError, ValueError):
                    out["build_number"] = 0
                rid = raw.get("run_id")
                out["run_id"] = rid.strip() if isinstance(rid, str) else ""
                sh = raw.get("sha")
                out["sha"] = sh.strip() if isinstance(sh, str) else ""
            break
        except (OSError, json.JSONDecodeError):
            continue
    return out


def load_game_config() -> dict:
    """Lê `game_config.json` junto a `main.py` ou, na raiz do projecto, `assets/game_config.json`."""
    base = Path(__file__).resolve().parent
    defaults: dict = {
        "player": {"hits_until_death": 1},
        "enemies": {
            k: _normalize_enemy_entry(v, k) for k, v in DEFAULT_ENEMIES_MERGE.items()
        },
        "characters": [dict(c) for c in DEFAULT_CHARACTERS],
        "spawn": {
            "intervalo_inicial_frames": 60,
            "intervalo_minimo_frames": 12,
            "velocidade_progressao": 1.0,
        },
        "escala_sprites": 1.15,
        "musica_selecao_personagem": None,
        "som": dict(DEFAULT_SOM),
    }
    paths: list[Path] = [base / "game_config.json"]
    if base.name != "assets" and (base / "assets").is_dir():
        paths.append(base / "assets" / "game_config.json")
    for path in paths:
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                patch = json.load(f)
            if isinstance(patch.get("player"), dict):
                defaults["player"].update(patch["player"])
            defaults["enemies"] = _merge_enemies_config(defaults["enemies"], patch.get("enemies"))
            if isinstance(patch.get("spawn"), dict):
                defaults["spawn"].update(patch["spawn"])
            if "escala_sprites" in patch:
                try:
                    defaults["escala_sprites"] = float(patch["escala_sprites"])
                except (TypeError, ValueError):
                    pass
            if isinstance(patch, dict) and "musica_selecao_personagem" in patch:
                ms = patch.get("musica_selecao_personagem")
                defaults["musica_selecao_personagem"] = (
                    ms.strip() if isinstance(ms, str) and ms.strip() else None
                )
            defaults["characters"] = load_characters_from_config(patch)
            defaults["som"] = _merge_som_config(dict(DEFAULT_SOM), patch.get("som"))
            au = patch.get("analytics_api_url")
            defaults["analytics_api_url"] = au.strip() if isinstance(au, str) else ""
            break
        except (OSError, json.JSONDecodeError):
            continue
    defaults["build"] = _load_build_params_dict()
    return defaults


def _resolve_audio_file(rel: str | None) -> Path | None:
    if not isinstance(rel, str) or not rel.strip():
        return None
    base = Path(__file__).resolve().parent
    name = rel.strip()
    p = Path(name)
    candidates: list[Path] = [base / name, base / "assets" / name]
    if p.name:
        candidates.append(base / "assets" / p.name)
    seen: set[str] = set()
    for cand in candidates:
        key = str(cand.resolve())
        if key in seen:
            continue
        seen.add(key)
        if cand.is_file():
            return cand
    return None


def _wasm_resolve_character_bgm_pack(character: dict | None) -> Path | None:
    """No WASM o `<audio>` não toca MIDI; usar `musica_fundo_wasm` (ogg/…), se existir no config."""
    if not isinstance(character, dict):
        return None
    raw = character.get("musica_fundo_wasm")
    if isinstance(raw, str) and raw.strip():
        p = _resolve_audio_file(raw.strip())
        if p is not None and p.is_file():
            return p
    return _resolve_audio_file(character.get("musica_fundo"))


def _pygame_bgm_stop() -> None:
    if _RUNS_IN_BROWSER_WASM:
        return
    try:
        pygame.mixer.music.stop()
    except (pygame.error, AttributeError):
        pass


def _pygame_bgm_play_file(path: Path, *, music_volume: float) -> bool:
    if _RUNS_IN_BROWSER_WASM:
        return False
    v = max(0.0, min(1.0, float(music_volume)))
    try_paths: list[Path] = [path]
    if path.suffix.lower() == ".aac":
        for ext in (".ogg",):
            alt = path.with_suffix(ext)
            if alt.is_file() and alt not in try_paths:
                try_paths.append(alt)
    for p in try_paths:
        try:
            pygame.mixer.music.load(str(p))
            pygame.mixer.music.set_volume(v)
            pygame.mixer.music.play(loops=-1)
            return True
        except (pygame.error, OSError, NotImplementedError):
            continue
    return False


def _load_scaled_png(filename: str, size: tuple[int, int]) -> pygame.Surface | None:
    """Local: main.py na raiz e PNGs em assets/ (e subpastas como novos/, backup/). Web (pygbag): idem."""
    base = Path(__file__).resolve().parent
    rel = Path(filename)
    candidates: list[Path] = [base / filename, base / "assets" / filename]
    # Mesmo nome em assets/backup/ (cópias antigas) se o caminho principal falhar
    if rel.name:
        candidates.append(base / "assets" / "backup" / rel.name)
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in candidates:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    for path in ordered:
        try:
            surf = pygame.image.load(str(path)).convert_alpha()
            surf = _first_square_frame(surf)
            surf = _crop_to_opaque_bounds(surf)
            if _RUNS_IN_BROWSER_WASM:
                return pygame.transform.scale(surf, size)
            return pygame.transform.smoothscale(surf, size)
        except (FileNotFoundError, OSError, pygame.error, ValueError):
            continue
    return None


def _load_character_portrait(character: dict, size: tuple[int, int]) -> pygame.Surface:
    """Sprite do personagem (menu e jogo); fallback para player.png."""
    cid = str(character.get("id", ""))
    sprite_name = character.get("sprite") or f"player_{cid}.png"
    surf = _load_scaled_png(str(sprite_name), size)
    if surf is None:
        surf = _load_scaled_png("player.png", size)
    if surf is None:
        fb = pygame.Surface(size)
        fb.fill((255, 215, 0))
        return fb
    return surf


BLACK = (20, 20, 25)
WHITE = (255, 255, 255)
GOLD = (255, 215, 0)
RED_OFF = (90, 45, 50)
GREEN_ON = (40, 85, 55)

PLAYER_SIZE = (55, 55)
ENEMY_SIZE = (37, 37)
BULLET_SIZE = (14, 14)
PLAYER_FALLBACK = (37, 37)
ENEMY_FALLBACK = (28, 28)
BULLET_FALLBACK = (10, 10)
SPAWN_PAD = 23

BASE_PLAYER_MOVE_SPEED = 3.0
BASE_BULLET_SPEED = 7.0
BASE_ENEMY_MOVE_SPEED = 1.5


def apply_escala_sprites_from_config(cfg: dict) -> None:
    """Define tamanhos de sprite a partir de `escala_sprites` no config (ex.: 1.15)."""
    global PLAYER_SIZE, ENEMY_SIZE, BULLET_SIZE, PLAYER_FALLBACK, ENEMY_FALLBACK, BULLET_FALLBACK, SPAWN_PAD
    scale = float(cfg.get("escala_sprites", 1.15))
    scale = max(0.5, min(2.5, scale))
    PLAYER_SIZE = (int(round(48 * scale)), int(round(48 * scale)))
    ENEMY_SIZE = (int(round(32 * scale)), int(round(32 * scale)))
    BULLET_SIZE = (int(round(12 * scale)), int(round(12 * scale)))
    PLAYER_FALLBACK = (int(round(32 * scale)), int(round(32 * scale)))
    ENEMY_FALLBACK = (int(round(24 * scale)), int(round(24 * scale)))
    BULLET_FALLBACK = (int(round(8 * scale)), int(round(8 * scale)))
    SPAWN_PAD = int(round(20 * scale))


def pick_enemy_type_id(score: int, enemies_norm: dict[str, dict]) -> str:
    """Escolhe tipo de inimigo conforme pontuação e `comeca_apos_pontos` de cada um."""
    eligible = [eid for eid, e in enemies_norm.items() if score >= int(e.get("comeca_apos_pontos", 0))]
    if not eligible:
        return next(iter(enemies_norm.keys()))
    return random.choice(eligible)


class Player(pygame.sprite.Sprite):
    def __init__(self, character: dict) -> None:
        super().__init__()
        self.character = character
        self.image = _load_character_portrait(character, PLAYER_SIZE)

        self.rect = self.image.get_rect(center=(WIDTH // 2, HEIGHT // 2))
        self.pos = pygame.Vector2(self.rect.center)
        vel = float(character.get("velocidade", 1.0))
        self.speed = BASE_PLAYER_MOVE_SPEED * vel

    def move(self, target_pos: tuple[float, float] | None) -> None:
        if not target_pos:
            return
        target_vec = pygame.Vector2(target_pos)
        direction = target_vec - self.pos
        if direction.length() > 5:
            direction = direction.normalize()
            self.pos += direction * self.speed
            self.rect.center = (int(self.pos.x), int(self.pos.y))


class Enemy(pygame.sprite.Sprite):
    def __init__(
        self,
        _player_pos: pygame.Vector2,
        *,
        enemy_profile: dict,
        cam_offset: pygame.Vector2 | None = None,
    ) -> None:
        super().__init__()
        sprite_file = str(enemy_profile.get("sprite", "enemy.png"))
        self.image = _load_scaled_png(sprite_file, ENEMY_SIZE)
        if self.image is None:
            self.image = _load_scaled_png("enemy.png", ENEMY_SIZE)
        if self.image is None:
            self.image = pygame.Surface(ENEMY_FALLBACK)
            self.image.fill((200, 0, 0))

        self.hits_to_destroy = max(1, int(enemy_profile.get("resistencia", 1)))
        self.hits_left = self.hits_to_destroy

        # Spawnar fora do viewport actual da câmara, em coordenadas de mundo.
        cx = cam_offset.x if cam_offset is not None else 0.0
        cy = cam_offset.y if cam_offset is not None else 0.0
        left, top = cx, cy
        right, bottom = cx + WIDTH, cy + HEIGHT

        side = random.choice(["t", "b", "l", "r"])
        if side == "t":
            self.pos = pygame.Vector2(random.uniform(left, right), top - SPAWN_PAD)
        elif side == "b":
            self.pos = pygame.Vector2(random.uniform(left, right), bottom + SPAWN_PAD)
        elif side == "l":
            self.pos = pygame.Vector2(left - SPAWN_PAD, random.uniform(top, bottom))
        else:
            self.pos = pygame.Vector2(right + SPAWN_PAD, random.uniform(top, bottom))

        self.rect = self.image.get_rect(center=(int(self.pos.x), int(self.pos.y)))
        vel = float(enemy_profile.get("velocidade", 1.0))
        self.speed = BASE_ENEMY_MOVE_SPEED * vel

    def take_bullet_hit(self, damage: float) -> bool:
        """Devolve True se o inimigo morreu (tiros esgotados)."""
        self.hits_left -= max(1, int(round(damage)))
        return self.hits_left <= 0

    def update(self, player_pos: pygame.Vector2) -> None:
        diff = pygame.Vector2(player_pos) - self.pos
        if diff.length_squared() < 1e-6:
            return
        direction = diff.normalize()
        self.pos += direction * self.speed
        self.rect.center = (int(self.pos.x), int(self.pos.y))


class Bullet(pygame.sprite.Sprite):
    def __init__(
        self,
        start_pos: pygame.Vector2,
        target_pos: pygame.Vector2,
        *,
        bullet_speed_mult: float = 1.0,
        damage: float = 1.0,
    ) -> None:
        super().__init__()
        self.image = _load_scaled_png("note.png", BULLET_SIZE)
        if self.image is None:
            self.image = pygame.Surface(BULLET_FALLBACK)
            self.image.fill(WHITE)

        self.rect = self.image.get_rect(center=(int(start_pos.x), int(start_pos.y)))
        self.pos = pygame.Vector2(start_pos)
        diff = pygame.Vector2(target_pos) - self.pos
        if diff.length_squared() < 1e-6:
            diff = pygame.Vector2(1, 0)
        self.dir = diff.normalize()
        self.speed = BASE_BULLET_SPEED * float(bullet_speed_mult)
        self.damage = float(damage)
        # Sem limite de tela: a bala morre depois de percorrer uma distância máxima.
        self.distance_left = float(max(WIDTH, HEIGHT)) * 1.2

    def update(self) -> None:
        step = self.dir * self.speed
        self.pos += step
        self.rect.center = (int(self.pos.x), int(self.pos.y))
        self.distance_left -= self.speed
        if self.distance_left <= 0:
            self.kill()


_SCROLL_TILE_MAIN: pygame.Surface | None = None
_SCROLL_TILE_FAR: pygame.Surface | None = None
_SCROLL_BG_USE_GRID_FALLBACK = False

_WORLD_GRID_FALLBACK = 64
_WORLD_GRID_COLOR = (34, 38, 48)
_WORLD_GRID_ACCENT = (48, 54, 70)


def _draw_world_background_grid(surface: pygame.Surface, cam_offset: pygame.Vector2) -> None:
    """Fundo a grelha (fallback se texturas RGB falharem no browser)."""
    surface.fill(BLACK)
    gs = _WORLD_GRID_FALLBACK
    start_x = -int(cam_offset.x) % gs
    start_y = -int(cam_offset.y) % gs
    for x in range(start_x - gs, WIDTH + gs, gs):
        col = _WORLD_GRID_ACCENT if ((x + int(cam_offset.x)) // gs) % 4 == 0 else _WORLD_GRID_COLOR
        pygame.draw.line(surface, col, (x, 0), (x, HEIGHT))
    for y in range(start_y - gs, HEIGHT + gs, gs):
        col = _WORLD_GRID_ACCENT if ((y + int(cam_offset.y)) // gs) % 4 == 0 else _WORLD_GRID_COLOR
        pygame.draw.line(surface, col, (0, y), (WIDTH, y))


def _surface_from_rgb_buffer(size: int, rgb: bytearray) -> pygame.Surface:
    """Uma cópia para textura — evita `set_at` por pixel (muito lento em pygbag/mobile)."""
    surf = pygame.image.frombytes(bytes(rgb), (size, size), "RGB")
    return surf.convert()


def _make_scroll_tile_far(size: int) -> pygame.Surface:
    """Textura repetível (céu / nébula distante) com período size em x e y."""
    buf = bytearray(size * size * 3)
    i = 0
    for y in range(size):
        for x in range(size):
            nx = 2 * math.pi * x / size
            ny = 2 * math.pi * y / size
            v = (
                0.34 * math.sin(nx * 1.4) * math.cos(ny * 1.1)
                + 0.28 * math.sin(nx * 2.8 + ny * 2.0)
                + 0.22 * math.sin(nx * 2.1 - ny * 2.9)
            )
            r = 9 + int(12 * v)
            g = 11 + int(14 * v)
            b = 24 + int(22 * v)
            sp = math.sin(x * 1.731 + y * 2.437)
            if sp > 0.91:
                r = min(255, r + 48)
                g = min(255, g + 52)
                b = min(255, b + 62)
            buf[i] = max(0, r)
            buf[i + 1] = max(0, g)
            buf[i + 2] = min(255, b)
            i += 3
    return _surface_from_rgb_buffer(size, buf)


def _make_scroll_tile_main(size: int) -> pygame.Surface:
    """Chão / arena repetível com variação tipo pedra e grelha subtil alinhada ao tile."""
    buf = bytearray(size * size * 3)
    i = 0
    for y in range(size):
        for x in range(size):
            nx = 2 * math.pi * x / size
            ny = 2 * math.pi * y / size
            stone = (
                0.3 * math.sin(nx * 2) * math.cos(ny * 2)
                + 0.22 * math.sin(nx * 4 + ny * 3)
                + 0.2 * math.sin(nx * 6) * math.sin(ny * 2)
                + 0.16 * math.sin((nx + ny) * 5)
            )
            r = 24 + int(26 * stone)
            g = 28 + int(28 * stone)
            b = 42 + int(40 * stone)
            buf[i] = max(0, min(255, r))
            buf[i + 1] = max(0, min(255, g))
            buf[i + 2] = max(0, min(255, b))
            i += 3
    surf = _surface_from_rgb_buffer(size, buf)
    gs = 44
    if size % gs == 0:
        line_c = (18, 22, 34)
        for x in range(0, size + 1, gs):
            pygame.draw.line(surf, line_c, (x, 0), (x, size), 1)
        for y in range(0, size + 1, gs):
            pygame.draw.line(surf, line_c, (0, y), (size, y), 1)
    return surf


def _scroll_background_tiles() -> tuple[pygame.Surface | None, pygame.Surface | None]:
    global _SCROLL_TILE_MAIN, _SCROLL_TILE_FAR, _SCROLL_BG_USE_GRID_FALLBACK
    if _SCROLL_BG_USE_GRID_FALLBACK:
        return None, None
    if _SCROLL_TILE_MAIN is None:
        try:
            _SCROLL_TILE_MAIN = _make_scroll_tile_main(176)
            _SCROLL_TILE_FAR = _make_scroll_tile_far(192)
        except (pygame.error, ValueError, TypeError, MemoryError, RuntimeError):
            _SCROLL_BG_USE_GRID_FALLBACK = True
            _SCROLL_TILE_MAIN = None
            _SCROLL_TILE_FAR = None
    return _SCROLL_TILE_MAIN, _SCROLL_TILE_FAR


def _blit_tiled_scroll(
    surface: pygame.Surface,
    tile: pygame.Surface,
    cam_offset: pygame.Vector2,
    parallax: float,
) -> None:
    """Repete `tile` no ecrã; deslocamento derivado de `cam_offset` com factor de parallax."""
    tw, th = tile.get_size()
    ox = float(cam_offset.x) * parallax
    oy = float(cam_offset.y) * parallax
    start_x = (-int(ox)) % tw
    start_y = (-int(oy)) % th
    for x in range(start_x - tw, WIDTH + tw, tw):
        for y in range(start_y - th, HEIGHT + th, th):
            surface.blit(tile, (x, y))


def _draw_world_background_wasm(surface: pygame.Surface, cam_offset: pygame.Vector2) -> None:
    """Fundo com scroll e poucos draw.rect (evita dezenas de draw.line por frame no canvas WASM)."""
    surface.fill(BLACK)
    bw = 40
    start_x = (-int(cam_offset.x)) % (bw * 2)
    for x in range(start_x - bw * 2, WIDTH + bw * 2, bw):
        i = (x + int(cam_offset.x)) // bw
        c = (32, 36, 48) if i % 2 == 0 else (22, 26, 36)
        pygame.draw.rect(surface, c, (x, 0, bw, HEIGHT))
    band_h = 10
    start_y = (-int(cam_offset.y)) % (band_h * 2)
    for y in range(start_y - band_h * 2, HEIGHT + band_h * 2, band_h):
        j = (y + int(cam_offset.y)) // band_h
        if j % 2 == 1:
            pygame.draw.rect(surface, (18, 22, 30), (0, y, WIDTH, band_h))


def draw_world_background(surface: pygame.Surface, cam_offset: pygame.Vector2) -> None:
    """Fundo infinito com scroll. No browser: rectas leves + update(); no desktop: tiles RGB."""
    if _RUNS_IN_BROWSER_WASM:
        _draw_world_background_wasm(surface, cam_offset)
        return
    main_tile, far_tile = _scroll_background_tiles()
    if main_tile is None or far_tile is None:
        _draw_world_background_grid(surface, cam_offset)
        return
    try:
        _blit_tiled_scroll(surface, far_tile, cam_offset, 0.24)
        _blit_tiled_scroll(surface, main_tile, cam_offset, 1.0)
    except (pygame.error, TypeError, ValueError):
        global _SCROLL_BG_USE_GRID_FALLBACK, _SCROLL_TILE_MAIN, _SCROLL_TILE_FAR
        _SCROLL_BG_USE_GRID_FALLBACK = True
        _SCROLL_TILE_MAIN = None
        _SCROLL_TILE_FAR = None
        _draw_world_background_grid(surface, cam_offset)


def draw_sprites_with_camera(
    surface: pygame.Surface,
    sprites: pygame.sprite.Group,
    cam_offset: pygame.Vector2,
) -> None:
    """Desenha sprites convertendo as suas posições de mundo para coordenadas de ecrã."""
    ox, oy = int(cam_offset.x), int(cam_offset.y)
    for sprite in sprites:
        surface.blit(sprite.image, (sprite.rect.x - ox, sprite.rect.y - oy))


def _shoot_toggle_rect() -> pygame.Rect:
    """Quadrado compacto no canto superior direito; o texto «Tiro» fica imediatamente à esquerda."""
    margin = 8
    side = 28
    return pygame.Rect(WIDTH - margin - side, 7, side, side)


def _music_toggle_rect() -> pygame.Rect:
    """Canto superior direito, por baixo do tiro, para não cobrir pontuação/vida à esquerda."""
    margin = 8
    w, h = 114, 30
    return pygame.Rect(WIDTH - w - margin, 40, w, h)


def _title_start_btn_rect() -> pygame.Rect:
    w, h = 168, 50
    return pygame.Rect((WIDTH - w) // 2, HEIGHT // 2 + 8, w, h)


def _game_over_buttons() -> tuple[pygame.Rect, pygame.Rect]:
    w, h = 220, 42
    gap = 10
    x = (WIDTH - w) // 2
    restart_y = HEIGHT // 2 + 18
    return (
        pygame.Rect(x, restart_y, w, h),
        pygame.Rect(x, restart_y + h + gap, w, h),
    )


def _draw_menu_button(
    surface: pygame.Surface,
    rect: pygame.Rect,
    label: str,
    font: pygame.font.Font,
    *,
    hover: bool = False,
    bg: tuple[int, int, int] = GREEN_ON,
) -> None:
    fill = (52, 62, 82) if hover else bg
    pygame.draw.rect(surface, fill, rect, border_radius=8)
    pygame.draw.rect(surface, WHITE, rect, 2, border_radius=8)
    txt = font.render(label, True, WHITE)
    surface.blit(txt, txt.get_rect(center=rect.center))


def _character_select_layout(chars: list[dict]) -> list[tuple[pygame.Rect, dict]]:
    top = 48
    gap = 6
    margin_x = 10
    w = WIDTH - 2 * margin_x
    n = max(1, len(chars))
    avail = HEIGHT - top - 14
    slot_h = max(52, (avail - (n - 1) * gap) // n)
    out: list[tuple[pygame.Rect, dict]] = []
    y = top
    for c in chars:
        out.append((pygame.Rect(margin_x, y, w, slot_h), c))
        y += slot_h + gap
    return out


def _analytics_device_label() -> str:
    if not _RUNS_IN_BROWSER_WASM:
        return "local"
    try:
        import js  # type: ignore[import-not-found]

        ua = str(js.navigator.userAgent).lower()
        mobile_markers = ("mobile", "android", "iphone", "ipad", "ipod", "webos")
        if any(m in ua for m in mobile_markers):
            return "navegador_celular"
        return "navegador_pc"
    except (ImportError, AttributeError, TypeError, ValueError):
        return "navegador_wasm"


def _analytics_api_base(cfg: dict) -> str | None:
    raw = cfg.get("analytics_api_url")
    if isinstance(raw, str):
        s = raw.strip()
        if s:
            return s.rstrip("/")
    return None


def _analytics_partida_extras(cfg: dict, character: dict | None) -> tuple[str | None, int | None]:
    """personagem (id) e número de build para POST /partidas."""
    pid: str | None = None
    if isinstance(character, dict):
        raw = character.get("id")
        if isinstance(raw, str) and raw.strip():
            pid = raw.strip()
    bn: int | None = None
    binfo = cfg.get("build")
    if isinstance(binfo, dict):
        try:
            bn = int(binfo.get("build_number", 0))
        except (TypeError, ValueError):
            bn = None
    return pid, bn


def _post_partida_sync(
    base_url: str,
    pontuacao: int,
    device: str,
    *,
    personagem: str | None = None,
    build: int | None = None,
) -> None:
    import json
    import urllib.error
    import urllib.request

    url = f"{base_url.rstrip('/')}/partidas"
    body_obj: dict[str, object] = {"pontuacao": pontuacao, "device": device}
    if personagem is not None:
        body_obj["personagem"] = personagem
    if build is not None:
        body_obj["build"] = build
    body = json.dumps(body_obj).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            resp.read(256)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        pass


async def _wasm_try_refresh_analytics_url(cfg: dict) -> dict:
    """Se não houver URL no FS embebido, lê `game_config.json` relativo à página (GitHub Pages / subpastas)."""
    if _analytics_api_base(cfg):
        return cfg
    try:
        import js  # type: ignore[import-not-found]
        from pyodide.ffi import to_js  # type: ignore[import-not-found]

        href = str(js.location.href).split("#")[0].split("?")[0]
        if href.endswith("index.html"):
            href = href[: -len("index.html")]
        if not href.endswith("/"):
            href = href + "/"
        cfg_url = urljoin(href, "game_config.json")
        r = await js.fetch(cfg_url, to_js({"cache": "no-store"}))
        if not r.ok:
            return cfg
        raw = await r.text()
        patch = json.loads(raw)
        au = patch.get("analytics_api_url")
        if isinstance(au, str) and au.strip():
            cfg["analytics_api_url"] = au.strip()
    except Exception:
        pass
    return cfg


async def _post_partida_wasm_fetch(
    base_url: str,
    pontuacao: int,
    device: str,
    *,
    personagem: str | None = None,
    build: int | None = None,
) -> None:
    """POST /partidas no browser: Pyodide usa `pyfetch`; pygbag só tem stub pyodide — usar `aio.fetch`."""
    import json

    url = f"{base_url.rstrip('/')}/partidas"
    body_obj: dict[str, object] = {"pontuacao": pontuacao, "device": device}
    if personagem is not None:
        body_obj["personagem"] = personagem
    if build is not None:
        body_obj["build"] = build

    pyfetch = None
    try:
        from pyodide.http import pyfetch as _pyfetch  # type: ignore[import-not-found]

        pyfetch = _pyfetch
    except ImportError:
        pass

    if pyfetch is not None:
        payload = json.dumps(body_obj).encode("utf-8")
        resp = await pyfetch(
            url,
            method="POST",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        await resp.bytes()
        return

    global _wasm_aio_fetch_handler
    from aio.fetch import RequestHandler  # type: ignore[import-not-found]

    if _wasm_aio_fetch_handler is None:
        _wasm_aio_fetch_handler = RequestHandler()
    await _wasm_aio_fetch_handler.post(url, body_obj)


async def _report_partida_async(
    base_url: str,
    pontuacao: int,
    device: str,
    *,
    personagem: str | None = None,
    build: int | None = None,
) -> None:
    try:
        if _RUNS_IN_BROWSER_WASM:
            await _post_partida_wasm_fetch(
                base_url,
                pontuacao,
                device,
                personagem=personagem,
                build=build,
            )
        else:
            await asyncio.to_thread(
                _post_partida_sync,
                base_url,
                pontuacao,
                device,
                personagem=personagem,
                build=build,
            )
    except Exception:
        pass


async def main() -> None:
    # No browser: não inicializar o mixer antes de um toque — Android/Chrome deixa o AudioContext suspenso.
    cfg = load_game_config()
    if _RUNS_IN_BROWSER_WASM:
        cfg = await _wasm_try_refresh_analytics_url(cfg)
    set_audio_vol_from_cfg(cfg)
    snd_hit: pygame.mixer.Sound | None
    snd_hurt: pygame.mixer.Sound | None
    snd_bg: pygame.mixer.Sound | None
    if _RUNS_IN_BROWSER_WASM:
        snd_hit = snd_hurt = snd_bg = None
    else:
        snd_hit, snd_hurt, snd_bg = load_procedural_sounds()
    apply_escala_sprites_from_config(cfg)
    enemies_cfg: dict[str, dict] = cfg["enemies"]
    characters: list[dict] = cfg["characters"]
    spawn_cfg = cfg["spawn"] if isinstance(cfg.get("spawn"), dict) else {}
    sel_music_rel = cfg.get("musica_selecao_personagem")
    sel_music_path = (
        _resolve_audio_file(str(sel_music_rel))
        if isinstance(sel_music_rel, str) and sel_music_rel.strip()
        else None
    )
    spawn_initial = max(8, int(spawn_cfg.get("intervalo_inicial_frames", 60)))
    spawn_min = int(spawn_cfg.get("intervalo_minimo_frames", 12))
    spawn_min = max(4, min(spawn_min, spawn_initial - 1))
    spawn_vel_prog = float(spawn_cfg.get("velocidade_progressao", 1.0))
    spawn_vel_prog = max(0.25, min(4.0, spawn_vel_prog))
    SPAWN_INTERVAL_SHRINK_BASE = 0.988

    game_phase = "start"
    selected_character: dict | None = None
    title_start_btn = _title_start_btn_rect()
    player_max_hp = 1

    player: Player | None = None
    all_sprites = pygame.sprite.Group()
    enemies = pygame.sprite.Group()
    bullets = pygame.sprite.Group()

    target_move_pos: tuple[float, float] | None = None
    spawn_timer = 0
    spawn_interval_frames = spawn_initial
    shoot_timer = 0
    score = 0
    player_hp = 1
    game_over = False
    shooting_enabled = True
    music_enabled = True
    font = pygame.font.SysFont(None, 32)
    font_btn = pygame.font.SysFont(None, 22)
    font_build = pygame.font.SysFont(None, 16)
    font_death = pygame.font.SysFont(None, 44)
    font_sel_title = pygame.font.SysFont(None, 30)
    font_sel_name = pygame.font.SysFont(None, 24)
    font_sel_sub = pygame.font.SysFont(None, 20)
    font_sel_stats = pygame.font.SysFont(None, 17)
    shoot_btn = _shoot_toggle_rect()
    music_btn = _music_toggle_rect()

    _sel_layout_preview = _character_select_layout(characters)
    _thumb_side = (
        max(40, min(58, _sel_layout_preview[0][0].height - 10))
        if _sel_layout_preview
        else 56
    )
    select_portraits: dict[str, pygame.Surface] = {
        str(ch["id"]): _load_character_portrait(ch, (_thumb_side, _thumb_side)) for ch in characters
    }

    if music_enabled:
        if not _RUNS_IN_BROWSER_WASM and sel_music_path is not None:
            _pygame_bgm_play_file(sel_music_path, music_volume=_audio_vol("musica_selecao_pygame"))
        elif _RUNS_IN_BROWSER_WASM and sel_music_path is not None:
            _wasm_selection_music_start(sel_music_path)

    # Texturas de fundo (só desktop); no browser a grelha é gerada em draw_world_background.
    await asyncio.sleep(0)
    if not _RUNS_IN_BROWSER_WASM:
        _scroll_background_tiles()

    CARD_BG = (38, 42, 52)
    CARD_LINE = (72, 78, 92)
    CARD_HOVER = (52, 62, 82)

    def pause_all_bg_music() -> None:
        _pygame_bgm_stop()
        _wasm_loop_bgm_pause_only()
        _wasm_web_audio_oscillator_stop_only()
        if snd_bg is not None:
            try:
                snd_bg.stop()
            except pygame.error:
                pass

    def resume_bg_music_for_phase() -> None:
        if not music_enabled:
            return
        if game_phase in ("start", "select"):
            if not _RUNS_IN_BROWSER_WASM and sel_music_path is not None:
                _pygame_bgm_play_file(sel_music_path, music_volume=_audio_vol("musica_selecao_pygame"))
            elif _RUNS_IN_BROWSER_WASM and sel_music_path is not None:
                _wasm_web_audio_prime()
                if not _wasm_loop_bgm_resume():
                    _wasm_selection_music_start(sel_music_path)
        elif game_phase == "playing" and selected_character is not None:
            if _RUNS_IN_BROWSER_WASM:
                bg_path = _wasm_resolve_character_bgm_pack(selected_character)
                _wasm_web_audio_prime()
                if not _wasm_loop_bgm_resume():
                    _wasm_web_audio_bg_start(pack_path=bg_path)
            else:
                bg_path = _resolve_audio_file(selected_character.get("musica_fundo"))
                _pygame_bgm_stop()
                if bg_path is not None and _pygame_bgm_play_file(
                    bg_path, music_volume=_audio_vol("musica_jogo_pygame")
                ):
                    pass
                elif snd_bg is not None and snd_bg.get_num_channels() == 0:
                    try:
                        snd_bg.play(loops=-1)
                    except pygame.error:
                        pass

    def begin_play(character: dict) -> None:
        nonlocal game_phase, selected_character, player_max_hp, player, all_sprites
        nonlocal enemies, bullets, target_move_pos, spawn_timer, spawn_interval_frames, shoot_timer
        nonlocal score, player_hp, game_over, shooting_enabled
        selected_character = character
        player_max_hp = max(1, int(character.get("resistencia", 1)))
        player = Player(character)
        all_sprites = pygame.sprite.Group(player)
        enemies = pygame.sprite.Group()
        bullets = pygame.sprite.Group()
        target_move_pos = None
        spawn_timer = 0
        spawn_interval_frames = spawn_initial
        shoot_timer = 0
        score = 0
        player_hp = player_max_hp
        game_over = False
        shooting_enabled = True
        game_phase = "playing"
        if music_enabled:
            if _RUNS_IN_BROWSER_WASM:
                bg_path = _wasm_resolve_character_bgm_pack(character)
                # Não adiar com create_task/sleep(0): o browser deixa de contar como gesto do utilizador.
                _wasm_web_audio_prime()
                _wasm_web_audio_bg_start(pack_path=bg_path)
            else:
                bg_path = _resolve_audio_file(character.get("musica_fundo"))
                _pygame_bgm_stop()
                if bg_path is not None and _pygame_bgm_play_file(
                    bg_path, music_volume=_audio_vol("musica_jogo_pygame")
                ):
                    pass
                elif snd_bg is not None and snd_bg.get_num_channels() == 0:
                    try:
                        snd_bg.play(loops=-1)
                    except pygame.error:
                        pass
        else:
            _wasm_web_audio_bg_stop()
            _pygame_bgm_stop()
            if snd_bg is not None:
                try:
                    snd_bg.stop()
                except pygame.error:
                    pass

    def reset_run() -> None:
        nonlocal player, all_sprites, enemies, bullets, target_move_pos
        nonlocal spawn_timer, spawn_interval_frames, shoot_timer, score, player_hp, game_over
        if selected_character is None:
            return
        player = Player(selected_character)
        all_sprites = pygame.sprite.Group(player)
        enemies = pygame.sprite.Group()
        bullets = pygame.sprite.Group()
        target_move_pos = None
        spawn_timer = 0
        spawn_interval_frames = spawn_initial
        shoot_timer = 0
        score = 0
        player_hp = player_max_hp
        game_over = False

    def go_to_character_select() -> None:
        nonlocal game_phase, game_over, player, selected_character, all_sprites, enemies, bullets
        game_phase = "select"
        game_over = False
        player = None
        selected_character = None
        all_sprites = pygame.sprite.Group()
        enemies = pygame.sprite.Group()
        bullets = pygame.sprite.Group()
        _wasm_web_audio_bg_stop()
        _pygame_bgm_stop()
        if snd_bg is not None:
            try:
                snd_bg.stop()
            except pygame.error:
                pass
        if music_enabled:
            resume_bg_music_for_phase()

    def _pick_character_screen_pos(pos: tuple[float, float]) -> dict | None:
        for rect, ch in _character_select_layout(characters):
            if rect.collidepoint(pos):
                return ch
        return None

    cam_offset = pygame.Vector2(0, 0)

    def screen_to_world(p: tuple[float, float]) -> tuple[float, float]:
        return (p[0] + cam_offset.x, p[1] + cam_offset.y)

    running = True
    while running:
        # A câmara é recalculada a cada frame: o jogador fica sempre no centro do ecrã.
        if player is not None:
            cam_offset.x = player.pos.x - WIDTH / 2
            cam_offset.y = player.pos.y - HEIGHT / 2

        _events = pygame.event.get()
        _wasm_skip_synth_mouse_click = False
        if _RUNS_IN_BROWSER_WASM:
            _wasm_skip_synth_mouse_click = any(e.type == pygame.FINGERDOWN for e in _events)

        for event in _events:
            if event.type == pygame.QUIT:
                running = False
                continue

            if game_phase == "start":
                if _mouse_click_is_left(event) and not (_RUNS_IN_BROWSER_WASM and _wasm_skip_synth_mouse_click):
                    mx, my = _mouse_down_pos_px(event)
                    if music_btn.collidepoint((mx, my)):
                        music_enabled = not music_enabled
                        if music_enabled:
                            resume_bg_music_for_phase()
                        else:
                            pause_all_bg_music()
                    elif title_start_btn.collidepoint((mx, my)):
                        go_to_character_select()
                elif event.type == pygame.FINGERDOWN:
                    fx, fy = _finger_event_to_px(event)
                    if music_btn.collidepoint((fx, fy)):
                        music_enabled = not music_enabled
                        if music_enabled:
                            resume_bg_music_for_phase()
                        else:
                            pause_all_bg_music()
                    elif title_start_btn.collidepoint((fx, fy)):
                        go_to_character_select()
                continue

            if game_phase == "select":
                if _mouse_click_is_left(event) and not (_RUNS_IN_BROWSER_WASM and _wasm_skip_synth_mouse_click):
                    mx, my = _mouse_down_pos_px(event)
                    if music_btn.collidepoint((mx, my)):
                        music_enabled = not music_enabled
                        if music_enabled:
                            resume_bg_music_for_phase()
                        else:
                            pause_all_bg_music()
                    else:
                        ch = _pick_character_screen_pos((mx, my))
                        if ch is not None:
                            begin_play(ch)
                elif event.type == pygame.FINGERDOWN:
                    fx, fy = _finger_event_to_px(event)
                    if music_btn.collidepoint((fx, fy)):
                        music_enabled = not music_enabled
                        if music_enabled:
                            resume_bg_music_for_phase()
                        else:
                            pause_all_bg_music()
                    else:
                        ch = _pick_character_screen_pos((fx, fy))
                        if ch is not None:
                            begin_play(ch)
                continue

            assert player is not None

            if game_over:
                restart_btn, new_char_btn = _game_over_buttons()
                if _mouse_click_is_left(event) and not (_RUNS_IN_BROWSER_WASM and _wasm_skip_synth_mouse_click):
                    mx, my = _mouse_down_pos_px(event)
                    if music_btn.collidepoint((mx, my)):
                        music_enabled = not music_enabled
                        if music_enabled:
                            resume_bg_music_for_phase()
                        else:
                            pause_all_bg_music()
                    elif restart_btn.collidepoint((mx, my)):
                        reset_run()
                    elif new_char_btn.collidepoint((mx, my)):
                        go_to_character_select()
                elif event.type == pygame.FINGERDOWN:
                    fx, fy = _finger_event_to_px(event)
                    if music_btn.collidepoint((fx, fy)):
                        music_enabled = not music_enabled
                        if music_enabled:
                            resume_bg_music_for_phase()
                        else:
                            pause_all_bg_music()
                    elif restart_btn.collidepoint((fx, fy)):
                        reset_run()
                    elif new_char_btn.collidepoint((fx, fy)):
                        go_to_character_select()
                continue

            if _mouse_click_is_left(event) and not (_RUNS_IN_BROWSER_WASM and _wasm_skip_synth_mouse_click):
                mx, my = _mouse_down_pos_px(event)
                if shoot_btn.collidepoint((mx, my)):
                    shooting_enabled = not shooting_enabled
                elif music_btn.collidepoint((mx, my)):
                    music_enabled = not music_enabled
                    if music_enabled:
                        resume_bg_music_for_phase()
                    else:
                        pause_all_bg_music()
                else:
                    target_move_pos = screen_to_world((mx, my))

            if event.type == pygame.MOUSEMOTION and pygame.mouse.get_pressed()[0]:
                mx, my = _mouse_motion_drag_pos_px(event)
                if not shoot_btn.collidepoint((mx, my)) and not music_btn.collidepoint((mx, my)):
                    target_move_pos = screen_to_world((mx, my))

            if event.type == pygame.FINGERDOWN:
                fx, fy = _finger_event_to_px(event)
                if shoot_btn.collidepoint((fx, fy)):
                    shooting_enabled = not shooting_enabled
                elif music_btn.collidepoint((fx, fy)):
                    music_enabled = not music_enabled
                    if music_enabled:
                        resume_bg_music_for_phase()
                    else:
                        pause_all_bg_music()
                else:
                    target_move_pos = screen_to_world((fx, fy))
            if event.type == pygame.FINGERMOTION:
                fx, fy = _finger_event_to_px(event)
                target_move_pos = screen_to_world((fx, fy))

        if game_phase == "playing" and not game_over and player is not None:
            player.move(target_move_pos)
            # Re-sincroniza a câmara após o movimento deste frame para spawnar
            # inimigos relativos ao viewport actual.
            cam_offset.x = player.pos.x - WIDTH / 2
            cam_offset.y = player.pos.y - HEIGHT / 2

            spawn_timer += 1
            if spawn_timer > spawn_interval_frames:
                eid = pick_enemy_type_id(score, enemies_cfg)
                profile = enemies_cfg[eid]
                enemy = Enemy(player.pos, enemy_profile=profile, cam_offset=cam_offset)
                enemies.add(enemy)
                all_sprites.add(enemy)
                spawn_timer = 0
                spawn_interval_frames = max(
                    spawn_min,
                    int(spawn_interval_frames * (SPAWN_INTERVAL_SHRINK_BASE**spawn_vel_prog)),
                )

            shoot_timer += 1
            if shooting_enabled and shoot_timer > 40 and enemies:
                closest = min(
                    enemies,
                    key=lambda e: pygame.Vector2(e.pos).distance_to(player.pos),
                )
                ch = selected_character or {}
                bullet = Bullet(
                    player.pos,
                    closest.pos,
                    bullet_speed_mult=float(ch.get("velocidade_tiro", 1.0)),
                    damage=float(ch.get("forca", 1.0)),
                )
                bullets.add(bullet)
                all_sprites.add(bullet)
                shoot_timer = 0

            enemies.update(player.pos)
            bullets.update()

            for bullet in list(bullets):
                struck = pygame.sprite.spritecollide(bullet, enemies, dokill=False)
                if not struck:
                    continue
                bullet.kill()
                if _RUNS_IN_BROWSER_WASM:
                    _wasm_web_audio_hit()
                elif snd_hit is not None:
                    try:
                        snd_hit.play()
                    except pygame.error:
                        pass
                enemy = struck[0]
                if enemy.take_bullet_hit(bullet.damage):
                    enemy.kill()
                    score += 1

            if pygame.sprite.spritecollide(player, enemies, False):
                player_hp -= 1
                if _RUNS_IN_BROWSER_WASM:
                    _wasm_web_audio_hurt()
                elif snd_hurt is not None:
                    try:
                        snd_hurt.play()
                    except pygame.error:
                        pass
                for e in enemies:
                    e.kill()
                for b in list(bullets):
                    b.kill()
                if player_hp <= 0:
                    game_over = True
                    base = _analytics_api_base(cfg)
                    if base is not None:
                        pid, bn = _analytics_partida_extras(cfg, selected_character)
                        await _report_partida_async(
                            base,
                            score,
                            _analytics_device_label(),
                            personagem=pid,
                            build=bn,
                        )

        if game_phase == "start":
            screen.fill(BLACK)
            title = font_sel_title.render("Pinga Ana Adventure", True, WHITE)
            screen.blit(title, title.get_rect(midtop=(WIDTH // 2, HEIGHT // 2 - 120)))
            subtitle = font_sel_sub.render("Sobreviva às hordas!", True, (190, 195, 210))
            screen.blit(subtitle, subtitle.get_rect(midtop=(WIDTH // 2, HEIGHT // 2 - 82)))
            mx, my = pygame.mouse.get_pos()
            _draw_menu_button(
                screen,
                title_start_btn,
                "Start",
                font_btn,
                hover=title_start_btn.collidepoint(mx, my),
            )
        elif game_phase == "select":
            screen.fill(BLACK)
            title = font_sel_title.render("Escolha o personagem", True, WHITE)
            screen.blit(title, title.get_rect(midtop=(WIDTH // 2, 8)))
            hint_sel = font_sel_stats.render("Toque num cartão para jogar", True, (170, 175, 190))
            screen.blit(hint_sel, hint_sel.get_rect(midtop=(WIDTH // 2, 36)))
            mx, my = pygame.mouse.get_pos()
            for rect, ch in _character_select_layout(characters):
                hover = rect.collidepoint(mx, my)
                bg = CARD_HOVER if hover else CARD_BG
                pygame.draw.rect(screen, bg, rect, border_radius=10)
                pygame.draw.rect(screen, CARD_LINE, rect, 1, border_radius=10)
                portrait = select_portraits.get(str(ch["id"]))
                pad = 8
                text_x = rect.x + pad
                if portrait is not None:
                    px = rect.x + pad
                    py = rect.y + (rect.height - portrait.get_height()) // 2
                    screen.blit(portrait, (px, py))
                    text_x = px + portrait.get_width() + 8
                name_s = font_sel_name.render(ch["name"], True, WHITE)
                screen.blit(name_s, (text_x, rect.y + 8))
                sub = font_sel_sub.render(ch.get("title", ""), True, (190, 195, 210))
                screen.blit(sub, (text_x, rect.y + 30))
                f, r, v, vt = (
                    ch.get("forca", 1),
                    ch.get("resistencia", 1),
                    ch.get("velocidade", 1),
                    ch.get("velocidade_tiro", 1),
                )
                stats = font_sel_stats.render(
                    f"Força {f}  ·  Res {r}  ·  Vel {v}  ·  Tiro {vt}",
                    True,
                    (200, 205, 220),
                )
                screen.blit(stats, (text_x, rect.bottom - 26))
        else:
            draw_world_background(screen, cam_offset)
            draw_sprites_with_camera(screen, all_sprites, cam_offset)

            score_txt = font.render(f"Pinga Score: {score}", True, WHITE)
            screen.blit(score_txt, (10, 10))

            hp_txt = font.render(f"Vida: {player_hp}/{player_max_hp}", True, WHITE)
            screen.blit(hp_txt, (10, 42))

            btn_bg = GREEN_ON if shooting_enabled else RED_OFF
            pygame.draw.rect(screen, btn_bg, shoot_btn, border_radius=6)
            pygame.draw.rect(screen, WHITE, shoot_btn, 2, border_radius=6)
            tiro_lbl = "Tiro ON" if shooting_enabled else "Tiro OFF"
            btn_txt = font_btn.render(tiro_lbl, True, WHITE)
            screen.blit(
                btn_txt,
                btn_txt.get_rect(midright=(shoot_btn.left - 6, shoot_btn.centery)),
            )

            if game_over:
                msg = font_death.render("vc morreu", True, WHITE)
                screen.blit(msg, msg.get_rect(center=(WIDTH // 2, HEIGHT // 2 - 52)))
                restart_btn, new_char_btn = _game_over_buttons()
                mx, my = pygame.mouse.get_pos()
                _draw_menu_button(
                    screen,
                    restart_btn,
                    "Recomeçar",
                    font_btn,
                    hover=restart_btn.collidepoint(mx, my),
                )
                _draw_menu_button(
                    screen,
                    new_char_btn,
                    "Novo personagem",
                    font_btn,
                    hover=new_char_btn.collidepoint(mx, my),
                    bg=(55, 75, 110),
                )

        btn_m_bg = GREEN_ON if music_enabled else RED_OFF
        pygame.draw.rect(screen, btn_m_bg, music_btn, border_radius=8)
        pygame.draw.rect(screen, WHITE, music_btn, 2, border_radius=8)
        m_lbl = "Música: ON" if music_enabled else "Música: OFF"
        m_txt = font_btn.render(m_lbl, True, WHITE)
        screen.blit(m_txt, m_txt.get_rect(center=music_btn.center))

        binfo = cfg.get("build")
        bn = 0
        if isinstance(binfo, dict):
            try:
                bn = int(binfo.get("build_number", 0))
            except (TypeError, ValueError):
                bn = 0
        build_line = font_build.render(f"build {bn}", True, (130, 135, 150))
        screen.blit(build_line, build_line.get_rect(bottomright=(WIDTH - 6, HEIGHT - 4)))

        if _RUNS_IN_BROWSER_WASM:
            clock.tick(0)
        else:
            clock.tick(60)
        _present_display()
        await asyncio.sleep(0)


asyncio.run(main())
