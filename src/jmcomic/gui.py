"""Local browser UI for JMComic.

The interface is rendered by the user's browser with custom HTML/CSS rather
than Tk/ttk native controls. The server binds to loopback only and delegates
the actual work to jmcomic's existing synchronous API.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import threading
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .gui_backend import JobManager


HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>JMComic Studio</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0e13; --rail: #0d1117; --panel: #11161f; --panel-2: #151b26;
      --line: #1e2632; --line-soft: #1a212c; --ink: #e6edf7; --muted: #96a5b8; --faint: #6e7d90;
      --blue: #2f81f7; --blue-soft: rgba(47, 129, 247, .14); --green: #3fb950; --amber: #d29922; --red: #f85149;
      --sans: "Noto Sans SC", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif;
      --mono: "Cascadia Mono", Consolas, monospace;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body { margin: 0; background: var(--bg); color: var(--ink); font: 14.5px/1.55 var(--sans); -webkit-font-smoothing: antialiased; }
    button { font-family: inherit; }
    ::selection { background: rgba(47, 129, 247, .35); }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-thumb { background: #263041; border: 3px solid transparent; background-clip: content-box; border-radius: 6px; }
    ::-webkit-scrollbar-thumb:hover { background: #334054; background-clip: content-box; }
    ::-webkit-scrollbar-track { background: transparent; }

    .app { display: grid; grid-template-columns: 216px minmax(0, 1fr); min-height: 100vh; }
    .sidebar { position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; padding: 14px 10px; background: var(--rail); border-right: 1px solid var(--line-soft); }
    .brand { display: flex; align-items: center; gap: 10px; padding: 4px 8px 16px; }
    .logo { width: 28px; height: 28px; display: grid; place-items: center; border-radius: 7px; background: linear-gradient(135deg, #3b8ef8, #1f6feb); color: #fff; font-size: 12px; font-weight: 800; }
    .brand .name { font-size: 14.5px; font-weight: 700; }
    .brand .ver { font: 9.5px var(--mono); color: var(--faint); }
    .nav { display: grid; gap: 2px; }
    .nav button { display: flex; align-items: center; gap: 10px; height: 36px; padding: 0 10px; border: 0; border-radius: 7px; background: none; color: #93a1b3; font-size: 13.5px; text-align: left; cursor: pointer; }
    .nav button:hover { background: var(--panel-2); color: var(--ink); }
    .nav button.active { background: #16233a; color: #dbeafe; }
    .nav svg { width: 16px; height: 16px; }
    .nav .count { margin-left: auto; font: 10px var(--mono); color: #8fb4dd; background: #172233; border-radius: 4px; padding: 1px 5px; }
    .side-foot { margin-top: auto; padding: 14px 8px 4px; border-top: 1px solid var(--line-soft); }
    .status-line { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--green); }
    .status-line i { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
    #statusChip.running i { animation: blink 1s steps(2, start) infinite; }
    @keyframes blink { 50% { opacity: .15; } }
    .out-label { margin-top: 10px; font-size: 11px; color: var(--faint); }
    .out-path { margin-top: 3px; font: 11px/1.5 var(--mono); color: #8896a8; word-break: break-all; }

    .workspace { display: flex; flex-direction: column; min-width: 0; }
    .topbar { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; gap: 12px; padding: 11px 16px; background: rgba(11, 14, 19, .92); border-bottom: 1px solid var(--line-soft); backdrop-filter: blur(10px); }
    .search-shell { flex: 1; display: flex; align-items: center; gap: 9px; height: 38px; padding: 0 12px; background: var(--panel); border: 1px solid #232c3a; border-radius: 8px; }
    .search-shell svg { width: 15px; height: 15px; color: var(--faint); }
    .search-shell input { flex: 1; min-width: 0; height: 100%; border: 0; outline: none; background: none; color: var(--ink); font: 14.5px var(--sans); }
    .search-shell input::placeholder { color: #5f6d7f; }
    .search-shell kbd { font: 10px var(--mono); color: var(--faint); border: 1px solid #2a3341; border-radius: 4px; padding: 2px 5px; }
    .icon-btn { width: 38px; height: 38px; display: grid; place-items: center; border: 1px solid #232c3a; border-radius: 8px; background: var(--panel); color: var(--muted); cursor: pointer; }
    .icon-btn:hover { color: var(--ink); border-color: #31405a; }
    .icon-btn svg { width: 16px; height: 16px; }
    .sys { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--faint); white-space: nowrap; }

    .content { display: grid; grid-template-columns: 306px minmax(0, 1fr) 308px; grid-template-areas: "results config inspector" "log log log"; gap: 12px; padding: 14px 16px 18px; align-items: start; }

    .card { background: var(--panel); border: 1px solid var(--line); border-radius: 9px; overflow: hidden; }
    .card-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 12px; border-bottom: 1px solid var(--line-soft); }
    .card-head h3 { display: flex; align-items: center; gap: 7px; margin: 0; font-size: 13.5px; font-weight: 600; }
    .card-head .meta { font-size: 11.5px; color: var(--faint); }
    .badge { display: grid; place-items: center; width: 18px; height: 18px; border-radius: 4px; background: var(--blue-soft); color: #79b8ff; font: 10px var(--mono); font-weight: 700; }
    .card-body { padding: 11px 12px; }

    .results-panel { grid-area: results; display: flex; flex-direction: column; align-self: stretch; }
    .filter-row { padding: 9px 11px; border-bottom: 1px solid var(--line-soft); }
    .seg { display: inline-flex; gap: 2px; padding: 2px; background: #0d121a; border: 1px solid var(--line); border-radius: 6px; }
    .seg button { height: 24px; padding: 0 9px; border: 0; border-radius: 4px; background: none; color: #8896a8; font-size: 12px; cursor: pointer; }
    .seg button.active { background: #1c2a40; color: #dbeafe; font-weight: 600; }
    .results { flex: 1; min-height: 230px; overflow: auto; padding: 5px; display: flex; flex-direction: column; }
    .result-item { display: flex; align-items: center; gap: 10px; width: 100%; padding: 7px; border: 0; border-radius: 7px; background: none; color: inherit; text-align: left; cursor: pointer; }
    .result-item:hover { background: var(--panel-2); }
    .result-item.selected { background: #16233a; }
    .cover { width: 42px; height: 56px; flex: 0 0 auto; border-radius: 4px; object-fit: cover; background: #1a2230; }
    .result-main { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 1px; }
    .result-title { font-size: 13.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .result-meta { font-size: 11px; color: var(--faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .result-arrow { color: #46536a; }
    .empty-note { margin: auto; padding: 16px 10px; max-width: 230px; text-align: center; color: var(--faint); font-size: 12.5px; line-height: 1.7; }

    .config { grid-area: config; display: grid; grid-template-columns: 1fr 1fr; grid-template-areas: "a c" "b d" "bar bar"; gap: 10px; align-items: stretch; }
    .card-a { grid-area: a; } .card-b { grid-area: b; } .card-c { grid-area: c; } .card-d { grid-area: d; }
    .field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
    .field > label { font-size: 12.5px; font-weight: 600; color: #a9b8ca; }
    .row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    input[type="text"], textarea { width: 100%; padding: 8px 10px; border: 1px solid #232c3a; border-radius: 7px; background: #0d121a; color: var(--ink); font: 13.5px/1.5 var(--sans); outline: none; }
    input[type="text"] { height: 36px; }
    textarea { min-height: 60px; resize: vertical; }
    input::placeholder, textarea::placeholder { color: #5b6879; }
    input:focus, textarea:focus { border-color: #3d6fae; box-shadow: 0 0 0 3px rgba(47, 129, 247, .13); }
    .hint { font-size: 11px; color: var(--faint); }
    .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
    .chips:empty { display: none; }
    .chip { padding: 1px 5px; border-radius: 4px; background: #172233; color: #8fb4dd; font: 10px var(--mono); }

    .fmt-row { display: flex; gap: 5px; }
    .fmt { flex: 1; height: 32px; border: 1px solid #232c3a; border-radius: 7px; background: #0d121a; color: #9fb0c3; font-size: 12px; cursor: pointer; }
    .fmt:hover { color: var(--ink); border-color: #31405a; }
    .fmt.active { border-color: #2f81f7; background: var(--blue-soft); color: #cfe6ff; font-weight: 600; }
    .mini-btn { flex: 0 0 auto; height: 36px; padding: 0 10px; border: 1px solid #232c3a; border-radius: 7px; background: #0d121a; color: var(--muted); font-size: 12.5px; cursor: pointer; }
    .mini-btn:hover { color: var(--ink); border-color: #31405a; }
    .mini-btn:disabled { opacity: .4; cursor: not-allowed; }
    .path-row { display: flex; gap: 5px; }
    .path-row input { flex: 1; }

    .setting { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 7px 0; border-top: 1px solid #171f2b; }
    .setting:first-of-type { border-top: 0; }
    .setting .label { font-size: 12.5px; color: #a9b8ca; }
    .mini-seg { display: inline-flex; gap: 2px; padding: 2px; background: #0d121a; border: 1px solid var(--line); border-radius: 6px; }
    .mini-seg button { height: 24px; padding: 0 8px; border: 0; border-radius: 4px; background: none; color: #8896a8; font-size: 11.5px; cursor: pointer; }
    .mini-seg button.active { background: #1c2a40; color: #dbeafe; }
    input[type="range"] { width: 118px; accent-color: #2f81f7; }
    .switch { position: relative; width: 34px; height: 19px; flex: 0 0 auto; }
    .switch input { position: absolute; opacity: 0; width: 100%; height: 100%; margin: 0; cursor: pointer; }
    .switch i { position: absolute; inset: 0; border-radius: 99px; background: #263041; transition: background .15s ease; pointer-events: none; }
    .switch i::after { content: ""; position: absolute; top: 2px; left: 2px; width: 15px; height: 15px; border-radius: 50%; background: #8ea3bd; transition: transform .15s ease, background .15s ease; }
    .switch input:checked + i { background: #1f6feb; }
    .switch input:checked + i::after { transform: translateX(15px); background: #fff; }

    .chapter-toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .radio { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; color: #b6c5d6; cursor: pointer; }
    .radio input { accent-color: #2f81f7; }
    .chapter-toolbar .meta { margin-left: auto; font-size: 11.5px; color: var(--faint); }
    .chapter-tools { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
    .chapter-tools input { flex: 1; height: 30px; }
    .chapter-tools .mini-btn { height: 30px; font-size: 11.5px; padding: 0 8px; }
    .chapter-box { margin-top: 8px; border: 1px solid var(--line); border-radius: 7px; max-height: 236px; overflow: auto; background: #0d121a; }
    .chapter-box:empty { display: none; }
    .chapter-row { display: flex; align-items: center; gap: 7px; padding: 4px 8px; border-bottom: 1px solid #151c27; font-size: 12px; }
    .chapter-row:last-child { border-bottom: 0; }
    .chapter-row:hover { background: #141c28; }
    .chapter-row input { accent-color: #2f81f7; }
    .chapter-row .idx { flex: 0 0 34px; color: #5a6879; font: 10.5px var(--mono); text-align: right; }
    .chapter-row .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .chapter-empty { padding: 12px; color: var(--faint); font-size: 12px; }

    .policy-list { margin: 0; padding: 0; list-style: none; display: grid; gap: 7px; }
    .policy-list li { position: relative; padding-left: 20px; font-size: 12px; color: #b6c5d6; line-height: 1.5; }
    .policy-list li::before { content: "✓"; position: absolute; left: 0; top: 0; color: #3fb950; font-size: 11px; }

    .action-bar { grid-area: bar; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; background: var(--panel); border: 1px solid var(--line); border-radius: 9px; flex-wrap: wrap; }
    .action-bar .summary { font-size: 12.5px; color: var(--muted); }
    .action-bar .summary b { color: #cfe6ff; font: 12.5px var(--mono); }
    .action-bar .buttons { display: flex; gap: 8px; flex-wrap: wrap; }
    .btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 36px; padding: 0 16px; border-radius: 7px; border: 1px solid transparent; font-size: 13px; font-weight: 600; cursor: pointer; }
    .btn svg { width: 14px; height: 14px; }
    .btn-primary { background: #2f81f7; color: #fff; }
    .btn-primary:hover:not(:disabled) { background: #1f6feb; }
    .btn-ghost { background: #0d121a; border-color: #232c3a; color: var(--ink); }
    .btn-ghost:hover:not(:disabled) { border-color: #31405a; }
    .btn-danger { background: #221318; border-color: #46212a; color: #ff8c94; }
    .btn-danger:hover:not(:disabled) { border-color: #6a2b36; }
    .btn:disabled { opacity: .38; cursor: not-allowed; }

    .inspector { grid-area: inspector; display: grid; gap: 10px; }
    .kv { display: grid; grid-template-columns: 74px minmax(0, 1fr); gap: 8px; font-size: 12.5px; padding: 3px 0; }
    .kv dt { color: var(--faint); }
    .kv dd { margin: 0; color: #c6d4e4; overflow-wrap: anywhere; }
    .kv dd b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }

    .album-card { display: none; }
    .album-card.show { display: block; }
    .album-head { display: flex; gap: 10px; }
    .album-head img { width: 52px; height: 70px; border-radius: 5px; object-fit: cover; background: #1a2230; }
    .album-head .t { min-width: 0; }
    .album-head .t b { display: block; font-size: 13px; line-height: 1.35; max-height: 2.7em; overflow: hidden; }
    .album-head .t span { display: block; margin-top: 3px; font-size: 11px; color: var(--faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .status-body { display: flex; align-items: center; gap: 12px; }
    .ring-wrap { position: relative; width: 58px; height: 58px; flex: 0 0 auto; }
    .ring-wrap svg { width: 58px; height: 58px; transform: rotate(-90deg); }
    .ring-track { fill: none; stroke: #1e2836; stroke-width: 4; }
    .ring-fill { fill: none; stroke: #2f81f7; stroke-width: 4; stroke-linecap: round; stroke-dasharray: 151; stroke-dashoffset: 151; transition: stroke-dashoffset .5s ease; }
    .ring-pct { position: absolute; inset: 0; display: grid; place-items: center; font: 12px var(--mono); color: #cfe6ff; }
    .status-text { min-width: 0; }
    .status-text b { display: block; font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .status-text span { display: block; margin-top: 2px; font-size: 11.5px; color: var(--faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .bar { height: 3px; margin-top: 10px; border-radius: 2px; background: #1a2230; overflow: hidden; }
    .bar i { display: block; width: 0; height: 100%; background: #2f81f7; transition: width .4s ease; }
    .stat-line { display: flex; justify-content: space-between; gap: 8px; padding: 2px 0; font-size: 11.5px; color: var(--faint); }
    .stat-line b { color: #cfe6ff; font: 11.5px var(--mono); font-weight: 500; }
    .stat-line .ell { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .monitor-actions { display: flex; gap: 6px; margin-top: 9px; }
    .monitor-actions .btn { flex: 1; height: 32px; font-size: 12px; padding: 0 8px; }

    .queue-row { display: flex; align-items: center; gap: 8px; padding: 5px 0; border-bottom: 1px solid #151c27; font-size: 11.5px; }
    .queue-row:last-child { border-bottom: 0; }
    .queue-row .qlabel { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #b6c5d6; }
    .queue-row .qpct { font: 10.5px var(--mono); color: #6f8db3; }
    .pill { font: 10px var(--mono); padding: 1px 5px; border-radius: 4px; background: #172233; color: #8fb4dd; white-space: nowrap; }
    .pill.done { background: #12251a; color: #7ee2a8; }
    .pill.failed { background: #2a1518; color: #ff9aa2; }
    .pill.paused { background: #2a2412; color: #e7c07b; }

    .done-card { display: none; }
    .done-card.show { display: block; }
    .done-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; margin: 6px 0 9px; }
    .done-item { background: #0d121a; border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; }
    .done-item span { display: block; font-size: 10px; color: var(--faint); }
    .done-item b { display: block; margin-top: 1px; font: 14px var(--mono); color: #dbeafe; }
    .done-title { display: flex; align-items: center; gap: 6px; color: #7ee2a8; font-size: 12px; font-weight: 600; }

    .log-card { grid-area: log; }
    .log-tools { display: flex; align-items: center; gap: 10px; }
    .log-tools label { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px; color: var(--faint); }
    .log-list { height: 196px; overflow: auto; padding: 7px 12px 10px; }
    .log-row { display: grid; grid-template-columns: 60px 58px minmax(0, 1fr); gap: 8px; padding: 2px 0; font: 12px/1.6 var(--mono); border-bottom: 1px solid #151c27; }
    .log-row:last-child { border-bottom: 0; }
    .log-row .lt { color: #5a6879; }
    .log-row .lv { color: #6f8db3; font-size: 10.5px; }
    .log-row .lm { color: #b6c5d6; overflow-wrap: anywhere; }
    .log-row.ok .lm { color: #7ee2a8; }
    .log-row.warn .lm { color: #e7c07b; }
    .log-row.err .lm { color: #ff9aa2; }
    .log-row.err .lv { color: #f85149; }
    .log-row.plain { grid-template-columns: minmax(0, 1fr); }

    .island { position: fixed; top: 10px; left: 50%; z-index: 50; display: flex; align-items: center; gap: 11px; padding: 8px 18px 8px 9px; border-radius: 999px; background: rgba(8, 10, 14, .94); border: 1px solid #2a3446; box-shadow: 0 18px 50px rgba(0, 0, 0, .55); backdrop-filter: blur(18px); pointer-events: none; transform: translate(-50%, -150%) scale(.86); opacity: 0; transition: transform .52s cubic-bezier(.22, 1.25, .3, 1), opacity .3s ease, border-color .3s ease; }
    .island.show { transform: translate(-50%, 0) scale(1); opacity: 1; }
    .island.done { border-color: rgba(63, 185, 80, .45); animation: island-pop .55s cubic-bezier(.2, 1.35, .3, 1); }
    .island.fail { border-color: rgba(248, 81, 73, .45); }
    .island .ring { width: 32px; height: 32px; }
    .island .track { fill: none; stroke: #26303f; stroke-width: 2.6; }
    .island .fill { fill: none; stroke: #3f8df8; stroke-width: 2.6; stroke-linecap: round; stroke-dasharray: 88; stroke-dashoffset: 88; transition: stroke-dashoffset .4s ease; }
    .island.spinning .fill { stroke-dasharray: 24 64; animation: island-spin .9s linear infinite; transform-origin: 18px 18px; }
    .island .check { fill: none; stroke: #3fb950; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; stroke-dasharray: 26; stroke-dashoffset: 26; transition: stroke-dashoffset .32s ease .12s; }
    .island.done .fill, .island.fail .fill { display: none; }
    .island.done .check { stroke-dashoffset: 0; }
    .island .island-text { display: flex; flex-direction: column; line-height: 1.25; }
    .island .island-text b { font-size: 12.5px; font-weight: 600; }
    .island .island-text span { font-size: 10.5px; color: #8b98ab; }
    @keyframes island-spin { to { transform: rotate(360deg); } }
    @keyframes island-pop { 0%, 100% { transform: translate(-50%, 0) scale(1); } 42% { transform: translate(-50%, 0) scale(1.06); } }

    .sr-only { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }

    @media (max-width: 1400px) {
      .content { grid-template-columns: 276px minmax(0, 1fr) 292px; }
      .config { grid-template-columns: 1fr; grid-template-areas: "a" "b" "c" "d" "bar"; }
    }
    @media (max-width: 1180px) {
      .content { grid-template-columns: 280px minmax(0, 1fr); grid-template-areas: "results config" "inspector inspector" "log log"; }
      .inspector { grid-template-columns: 1fr 1fr; }
      .album-card { grid-column: 1 / -1; }
    }
    @media (max-width: 900px) {
      .app { grid-template-columns: 1fr; }
      .sidebar { display: none; }
      .content { grid-template-columns: 1fr; grid-template-areas: "results" "config" "inspector" "log"; padding: 10px; }
      .inspector { grid-template-columns: 1fr; }
      .row-2 { grid-template-columns: 1fr; }
      .sys { display: none; }
    }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }

    /* ---------- 2026-10 视觉调整：更大的字号 + 石墨蓝灰配色 ---------- */
    :root {
      --bg: #161a21;
      --rail: #12161c;
      --panel: #1b2029;
      --panel-2: #232a35;
      --line: #2c3441;
      --line-soft: #232a35;
      --ink: #eaedf2;
      --muted: #a4adbb;
      --faint: #7e8898;
      --blue: #8ab4f8;
      --blue-soft: rgba(138, 180, 248, .14);
      --green: #7cc4a4;
      --amber: #d8b26a;
      --red: #e08a8a;
    }
    body { font-size: 16px; line-height: 1.6; background-image: radial-gradient(900px 520px at 18% -10%, #1d2330 0%, transparent 60%), radial-gradient(760px 420px at 92% -6%, #1a2433 0%, transparent 55%); background-attachment: fixed; }
    .nav button { height: 40px; font-size: 14.5px; }
    .card-head { padding: 11px 14px; }
    .card-head h3 { font-size: 15px; }
    .card-head .meta { font-size: 12.5px; }
    .card-body { padding: 13px 14px; }
    .result-title { font-size: 15px; }
    .result-meta { font-size: 12px; }
    .empty-note { font-size: 13.5px; }
    .field > label { font-size: 13.5px; }
    input[type="text"], textarea { font-size: 14.5px; }
    input[type="text"] { height: 38px; }
    .mini-btn { height: 38px; font-size: 13px; }
    .fmt { height: 34px; font-size: 13px; }
    .setting .label { font-size: 13.5px; }
    .mini-seg button { height: 26px; font-size: 12px; }
    .radio { font-size: 13px; }
    .chapter-tools input { height: 32px; }
    .chapter-row { font-size: 12.5px; padding: 5px 9px; }
    .chapter-toolbar .meta, .hint { font-size: 12px; }
    .policy-list li { font-size: 12.5px; }
    .action-bar .summary { font-size: 13.5px; }
    .btn { height: 40px; font-size: 14px; }
    .kv { font-size: 13.5px; }
    .stat-line { font-size: 12.5px; }
    .log-row { font-size: 12.5px; }
    .log-tools label { font-size: 12px; }
    .queue-row { font-size: 12.5px; }
    .album-head .t b { font-size: 14px; }
    .status-text b { font-size: 14px; }
    .status-text span { font-size: 12.5px; }
    :root { --bg: #181d26; --rail: #141922; --panel: #1f2531; --panel-2: #283040; --line: #333d4d; --line-soft: #283040; }
    body { background-color: #181d26; }
    input[type="text"], textarea, .chapter-box, .chapter-tools input, .log-list { background: #151b25; }
    .log-list { height: 170px; }
    :root {
      --bg: #201b17; --rail: #1a1613; --panel: #29231e; --panel-2: #332c25;
      --line: #453b31; --line-soft: #332c25;
      --ink: #f3ece3; --muted: #b7a99a; --faint: #8f8274;
      --blue: #e0a86a; --blue-soft: rgba(224, 168, 106, .16);
      --green: #93c2a1; --amber: #e2b872; --red: #e0907e;
    }
    body {
      font-size: 17px; line-height: 1.65; background-color: #201b17;
      background-image: radial-gradient(900px 520px at 18% -10%, #2a221c 0%, transparent 62%), radial-gradient(760px 420px at 92% -6%, #241d18 0%, transparent 55%);
    }
    .nav button { height: 42px; font-size: 15.5px; }
    .card-head h3 { font-size: 16px; }
    .card-head .meta { font-size: 13px; }
    .result-title { font-size: 16px; }
    .result-meta { font-size: 12.5px; }
    .field > label { font-size: 14.5px; }
    input[type="text"], textarea { font-size: 15.5px; }
    input[type="text"] { height: 41px; }
    .mini-btn { height: 41px; font-size: 14px; }
    .fmt { height: 37px; font-size: 14px; }
    .setting .label { font-size: 14.5px; }
    .radio { font-size: 14px; }
    .chapter-row { font-size: 13.5px; padding: 6px 9px; }
    .hint, .chapter-toolbar .meta { font-size: 13px; }
    .policy-list li { font-size: 13.5px; }
    .action-bar .summary { font-size: 14.5px; }
    .btn { height: 43px; font-size: 15px; }
    .kv { font-size: 14.5px; }
    .stat-line { font-size: 13.5px; }
    .log-row { font-size: 13.5px; }
    .queue-row { font-size: 13.5px; }
    .album-head .t b { font-size: 15px; }
    .status-text b { font-size: 15px; }
    .status-text span { font-size: 13.5px; }
    input[type="text"], textarea, .chapter-box, .chapter-tools input, .log-list { background: #241e19; }
    .btn-primary { background: #d99a5b; color: #241a12; }
    .btn-primary:hover:not(:disabled) { background: #e6a96b; }
    .fmt.active { border-color: #d99a5b; background: rgba(217, 154, 91, .16); color: #f6e2ca; }
    .seg button.active, .mini-seg button.active { background: #3a2f25; color: #f6e2ca; }
    .badge { background: rgba(217, 154, 91, .16); color: #e8b47e; }
    .ring-fill, .bar i { stroke: #d99a5b; background: #d99a5b; }
    .island .fill { stroke: #d99a5b; }
    .album-card, .card { box-shadow: 0 1px 0 rgba(255, 255, 255, .02) inset; }

    /* ---------- 淡蓝浅色主题 ---------- */
    :root {
      color-scheme: light;
      --bg: #e9f0f8; --rail: #f4f8fc; --panel: #ffffff; --panel-2: #eef4fb;
      --line: #d5e0ec; --line-soft: #e3ebf5;
      --ink: #1e2a38; --muted: #5a6b7e; --faint: #8696a8;
      --blue: #4a86e8; --blue-soft: rgba(74, 134, 232, .12);
      --green: #4f9d76; --amber: #b07a24; --red: #c25b54;
    }
    body { background-color: #e9f0f8; color: #1e2a38; background-image: radial-gradient(900px 520px at 16% -10%, #ffffff 0%, transparent 62%), radial-gradient(760px 420px at 94% -8%, #dce9f7 0%, transparent 55%); }
    .sidebar { background: #f4f8fc; }
    .nav button { color: #5a6b7e; }
    .nav button.active { background: #dbe8f8; color: #1e2a38; }
    .nav .count { background: #dbe8f8; color: #3d6fae; }
    .card, .action-bar { background: #ffffff; border-color: #d5e0ec; box-shadow: 0 1px 2px rgba(30, 42, 56, .04); }
    .card-head { border-color: #e3ebf5; }
    .card-head h3, .field > label, .setting .label, .kv dd, .result-title { color: #1e2a38; }
    .kv dt, .stat-line, .hint, .card-head .meta, .panel-note { color: #7d8ea1; }
    .stat-line b, .kv dd b { color: #2a3a4d; }
    input[type="text"], textarea, .chapter-box, .chapter-tools input, .log-list { background: #f8fbfe; color: #1e2a38; border-color: #d5e0ec; }
    input::placeholder, textarea::placeholder { color: #9aa9b8; }
    .result-item:hover { background: #eef4fb; }
    .result-item.selected { background: #dbe8f8; }
    .seg, .mini-seg, .mode { background: #eef4fb; border-color: #d5e0ec; }
    .seg button, .mini-seg button { color: #5a6b7e; }
    .seg button.active, .mini-seg button.active { background: #ffffff; color: #1e2a38; box-shadow: 0 1px 2px rgba(30, 42, 56, .08); }
    .fmt { background: #ffffff; border-color: #d5e0ec; color: #4a5a6c; }
    .fmt.active { border-color: #4a86e8; background: #eaf2fd; color: #24507f; }
    .btn-ghost { background: #ffffff; border-color: #d5e0ec; color: #1e2a38; }
    .btn-primary { background: #4a86e8; color: #fff; }
    .btn-primary:hover:not(:disabled) { background: #3d76d6; }
    .btn-danger { background: #fdf0ef; border-color: #eccac7; color: #b34a43; }
    .mini-btn { background: #ffffff; border-color: #d5e0ec; color: #5a6b7e; }
    .log-row .lm, .log-row .lt { color: #7d8ea1; }
    .log-row .lt { color: #9aa9b8; }
    .log-row.ok .lm { color: #3f8a63; } .log-row.warn .lm { color: #a06f1e; } .log-row.err .lm { color: #b34a43; }
    .policy-list li { color: #4a5a6c; }
    .switch i { background: #ccd8e6; }
    .switch input:checked + i { background: #4a86e8; }
    .queue-row { border-color: #e8eff7; }
    .album-head .t b { color: #1e2a38; }
    .status-text b { color: #1e2a38; }
    .ring-track { stroke: #e3ebf5; }
    .ring-fill, .bar i { stroke: #4a86e8; background: #4a86e8; }
    .badge { background: rgba(74, 134, 232, .12); color: #3d76d6; }
    .island { background: rgba(28, 38, 50, .95); }
    .search-shell { background: #ffffff; border-color: #d5e0ec; }
    .search-shell input { color: #1e2a38; }
    .topbar { background: rgba(233, 240, 248, .92); border-color: #dbe5f0; }
    .side-foot { border-color: #e3ebf5; }
    .out-path { color: #7d8ea1; }
    .action-bar .summary b { color: #2a3a4d; }
    .action-bar .summary { color: #5a6b7e; }
    .setting { border-top-color: #e8eff7; }
    .chapter-row { border-bottom-color: #e8eff7; }
    .log-row { border-bottom-color: #e8eff7; }
    .queue-row { border-bottom-color: #e8eff7; }
    #qualityValue { color: #2a3a4d; }
    .setting .label { color: #2a3a4d; }
    input[type="range"] { accent-color: #4a86e8; }

    /* ---------- 深海蓝：#021024 #052659 #5483B3 #7DA0CA #C1E8FF ---------- */
    :root {
      color-scheme: dark;
      --bg: #021024; --rail: #041a38; --panel: #052659; --panel-2: #0a3570;
      --line: #24497a; --line-soft: #123466;
      --ink: #c1e8ff; --muted: #7da0ca; --faint: #5483b3;
      --blue: #7da0ca; --blue-soft: rgba(125, 160, 202, .18);
      --green: #8fc7c9; --amber: #c9b27d; --red: #d99a9a;
    }
    body { background-color: #021024; color: #c1e8ff; background-image: radial-gradient(900px 520px at 16% -10%, #0a3570 0%, transparent 62%), radial-gradient(760px 420px at 94% -8%, #052659 0%, transparent 55%); }
    .sidebar { background: #041a38; }
    .nav button { color: #7da0ca; }
    .nav button:hover { background: #0a3570; color: #c1e8ff; }
    .nav button.active { background: #0a3570; color: #c1e8ff; }
    .nav .count { background: #123466; color: #c1e8ff; }
    .card, .action-bar { background: #052659; border-color: #24497a; box-shadow: none; }
    .card-head { border-color: #123466; }
    .card-head h3, .field > label, .setting .label, .kv dd, .result-title, .album-head .t b, .status-text b { color: #c1e8ff; }
    .kv dt, .stat-line, .hint, .card-head .meta, .panel-note, .out-path { color: #7da0ca; }
    .stat-line b, .kv dd b, #qualityValue { color: #c1e8ff; }
    input[type="text"], textarea, .chapter-box, .chapter-tools input, .log-list { background: #021024; color: #c1e8ff; border-color: #24497a; }
    input::placeholder, textarea::placeholder { color: #5483b3; }
    .setting, .chapter-row, .log-row, .queue-row { border-color: #123466; }
    .result-item:hover { background: #0a3570; }
    .result-item.selected { background: #123466; }
    .seg, .mini-seg, .mode { background: #021024; border-color: #24497a; }
    .seg button, .mini-seg button { color: #7da0ca; }
    .seg button.active, .mini-seg button.active { background: #123466; color: #c1e8ff; box-shadow: none; }
    .fmt { background: #021024; border-color: #24497a; color: #7da0ca; }
    .fmt.active { border-color: #7da0ca; background: #123466; color: #c1e8ff; }
    .btn-ghost { background: #021024; border-color: #24497a; color: #c1e8ff; }
    .btn-primary { background: #5483b3; color: #021024; }
    .btn-primary:hover:not(:disabled) { background: #7da0ca; }
    .btn-danger { background: #2a1420; border-color: #4a2436; color: #d99a9a; }
    .mini-btn { background: #021024; border-color: #24497a; color: #7da0ca; }
    .log-row .lm { color: #a8cbe6; }
    .log-row .lt { color: #5483b3; }
    .log-row.ok .lm { color: #8fc7c9; } .log-row.warn .lm { color: #c9b27d; } .log-row.err .lm { color: #d99a9a; }
    .policy-list li { color: #a8cbe6; }
    .switch i { background: #123466; }
    .switch input:checked + i { background: #5483b3; }
    .ring-track { stroke: #123466; }
    .ring-fill, .bar i { stroke: #7da0ca; background: #7da0ca; }
    .badge { background: rgba(125, 160, 202, .18); color: #c1e8ff; }
    .search-shell { background: #052659; border-color: #24497a; }
    .search-shell input { color: #c1e8ff; }
    .topbar { background: rgba(2, 16, 36, .92); border-color: #123466; }
    .side-foot { border-color: #123466; }
    .island { background: rgba(2, 16, 36, .95); border-color: #24497a; }
    .action-bar .summary { color: #7da0ca; }
    .action-bar .summary b { color: #c1e8ff; }
    /* ---------- 莫兰迪色 ---------- */
    :root {
      color-scheme: light;
      --bg: #e7e5df; --rail: #efede7; --panel: #f7f5f0; --panel-2: #eeebe4;
      --line: #d6d1c7; --line-soft: #e2ded5;
      --ink: #3b3a36; --muted: #6f6c65; --faint: #9c988f;
      --blue: #8fa39a; --blue-soft: rgba(143, 163, 154, .18);
      --green: #8fa39a; --amber: #b9a27a; --red: #b98d88;
    }
    body { background-color: #e7e5df; color: #3b3a36; background-image: radial-gradient(900px 520px at 16% -10%, #f3f1ec 0%, transparent 62%), radial-gradient(760px 420px at 94% -8%, #eeeae3 0%, transparent 55%); }
    .sidebar { background: #efede7; }
    .nav button { color: #6f6c65; }
    .nav button:hover { background: #e6e2da; color: #3b3a36; }
    .nav button.active { background: #dcd8ce; color: #3b3a36; }
    .nav .count { background: #e2ded5; color: #6f6c65; }
    .card, .action-bar { background: #f7f5f0; border-color: #d6d1c7; box-shadow: 0 1px 2px rgba(59, 58, 54, .04); }
    .card-head { border-color: #e2ded5; }
    .card-head h3, .field > label, .setting .label, .kv dd, .result-title, .album-head .t b, .status-text b { color: #3b3a36; }
    .kv dt, .stat-line, .hint, .card-head .meta, .panel-note, .out-path { color: #8a867d; }
    .stat-line b, .kv dd b, #qualityValue { color: #3b3a36; }
    input[type="text"], textarea, .chapter-box, .chapter-tools input, .log-list { background: #fbf9f5; color: #3b3a36; border-color: #d6d1c7; }
    input::placeholder, textarea::placeholder { color: #a8a49a; }
    .setting, .chapter-row, .log-row, .queue-row { border-color: #e6e2da; }
    .result-item:hover { background: #efebe4; }
    .result-item.selected { background: #e4ded4; }
    .seg, .mini-seg, .mode { background: #eeebe4; border-color: #d6d1c7; }
    .seg button, .mini-seg button { color: #6f6c65; }
    .seg button.active, .mini-seg button.active { background: #fbf9f5; color: #3b3a36; box-shadow: 0 1px 2px rgba(59, 58, 54, .06); }
    .fmt { background: #fbf9f5; border-color: #d6d1c7; color: #6f6c65; }
    .fmt.active { border-color: #8fa39a; background: #e8efea; color: #3f5a4e; }
    .btn-ghost { background: #fbf9f5; border-color: #d6d1c7; color: #3b3a36; }
    .btn-primary { background: #7f938a; color: #f7f5f0; }
    .btn-primary:hover:not(:disabled) { background: #8fa39a; }
    .btn-danger { background: #f7eeec; border-color: #e3d2cf; color: #a06f68; }
    .mini-btn { background: #fbf9f5; border-color: #d6d1c7; color: #6f6c65; }
    .log-row .lm { color: #4a4944; }
    .log-row .lt { color: #9c988f; }
    .log-row.ok .lm { color: #6f8a7d; } .log-row.warn .lm { color: #a0855a; } .log-row.err .lm { color: #a06f68; }
    .policy-list li { color: #4a4944; }
    .switch i { background: #d6d1c7; }
    .switch input:checked + i { background: #8fa39a; }
    .ring-track { stroke: #e2ded5; }
    .ring-fill, .bar i { stroke: #8fa39a; background: #8fa39a; }
    .badge { background: rgba(143, 163, 154, .2); color: #5d7a6c; }
    .search-shell { background: #f7f5f0; border-color: #d6d1c7; }
    .search-shell input { color: #3b3a36; }
    .topbar { background: rgba(231, 229, 223, .94); border-color: #dcd8ce; }
    .side-foot { border-color: #dcd8ce; }
    .island { background: rgba(59, 58, 54, .94); border-color: #6f6c65; }
    .action-bar .summary { color: #6f6c65; }
    .action-bar .summary b { color: #3b3a36; }
    .bar { background: #e2ded5; }
    input[type="range"] { accent-color: #8fa39a; }
    #qualityValue { color: #3b3a36 !important; }
    /* ---------- 柔和化：降对比、加圆角、去硬线 ---------- */
    :root {
      --bg: #ece9e2; --rail: #f1eee7; --panel: #faf8f3; --panel-2: #f0ece4;
      --line: #e8e3d8; --line-soft: #efeae0;
      --ink: #4a4741; --muted: #8b857a; --faint: #a9a298;
      --blue: #93a79d; --blue-soft: rgba(147, 167, 157, .16);
    }
    body { background-color: #ece9e2; color: #4a4741; background-image: radial-gradient(900px 520px at 16% -10%, #f7f4ee 0%, transparent 62%), radial-gradient(760px 420px at 94% -8%, #f2eee6 0%, transparent 55%); }
    .card, .action-bar { border-color: #e8e3d8; border-radius: 12px; box-shadow: 0 2px 10px rgba(90, 84, 72, .05); }
    .card-head { border-bottom-color: #efeae0; padding: 13px 15px; }
    .card-head h3 { font-weight: 600; }
    .card-body { padding: 14px 15px; }
    input[type="text"], textarea, .chapter-box, .chapter-tools input, .log-list { border-color: #e2ddd2; border-radius: 9px; background: #fdfcfa; }
    .setting, .chapter-row, .log-row, .queue-row { border-color: #f3efe8; }
    .setting { border-top: 0; }
    .btn { border-radius: 9px; }
    .btn-primary { background: #93a79d; color: #fdfbf7; }
    .fmt, .mini-btn, .seg, .mini-seg, .mode { border-radius: 8px; }
    .nav button { border-radius: 9px; }
    .nav button.active { background: #e6e0d5; }
    .result-item { border-radius: 9px; }
    .topbar { background: rgba(236, 233, 226, .9); border-color: #e8e3d8; }
    .badge { background: #eef1ec; color: #6f8578; border-radius: 6px; }
    .island { border-radius: 22px; }
    .hint, .card-head .meta, .panel-note { color: #a09a8f; }
    /* ---------- 玻璃质感 ---------- */
    body::before {
      content: ""; position: fixed; inset: -12%; z-index: 0; pointer-events: none;
      background:
        radial-gradient(42% 34% at 16% 20%, rgba(143, 163, 154, .78), transparent 70%),
        radial-gradient(38% 30% at 84% 16%, rgba(159, 176, 201, .72), transparent 70%),
        radial-gradient(44% 36% at 72% 84%, rgba(194, 164, 158, .6), transparent 70%),
        radial-gradient(34% 28% at 24% 86%, rgba(205, 190, 160, .6), transparent 70%);
      filter: none;
    }
    .app { position: relative; z-index: 1; }
    .sidebar { background: rgba(250, 248, 243, .55); backdrop-filter: none; }
    .topbar { background: rgba(240, 237, 230, .62); backdrop-filter: none; }
    .card, .action-bar {
      background: linear-gradient(160deg, rgba(255, 255, 255, .6), rgba(255, 255, 255, .34));
      backdrop-filter: none;
      -webkit-backdrop-filter: none;
      border: 1px solid rgba(255, 255, 255, .72);
      border-radius: 14px;
      box-shadow: 0 10px 30px rgba(90, 84, 72, .08), inset 0 1px 0 rgba(255, 255, 255, .85);
    }
    .card-head { border-bottom-color: rgba(255, 255, 255, .55); }
    input[type="text"], textarea, .chapter-tools input { background: rgba(255, 255, 255, .58); border-color: rgba(255, 255, 255, .7); }
    .chapter-box, .log-list { background: rgba(255, 255, 255, .42); border-color: rgba(255, 255, 255, .6); }
    .setting, .chapter-row, .log-row, .queue-row { border-color: rgba(255, 255, 255, .45); }
    .nav button.active { background: rgba(255, 255, 255, .55); }
    .seg, .mini-seg, .mode { background: rgba(255, 255, 255, .42); border-color: rgba(255, 255, 255, .6); }
    .fmt, .btn-ghost, .mini-btn { background: rgba(255, 255, 255, .55); border-color: rgba(255, 255, 255, .7); }
    .fmt.active { background: rgba(147, 167, 157, .22); border-color: rgba(147, 167, 157, .7); }
    .btn-primary { background: rgba(127, 147, 138, .92); box-shadow: 0 6px 18px rgba(111, 138, 125, .25); }
    .island { background: rgba(74, 71, 65, .72); backdrop-filter: none; border-color: rgba(255, 255, 255, .25); }
    .badge { background: rgba(147, 167, 157, .18); }
    /* ---------- 右侧信息区放大与留白 ---------- */
    @media (min-width: 1400px) { .content { grid-template-columns: 300px minmax(0, 1fr) 344px; } }
    .kv { font-size: 15px; padding: 5px 0; grid-template-columns: 88px minmax(0, 1fr); }
    .kv dd { line-height: 1.5; }
    .stat-line { font-size: 15px; padding: 7px 0; line-height: 1.6; }
    .stat-line b { font-size: 15px; }
    .stat-line .ell { max-width: 210px; }
    .queue-row { font-size: 15px; padding: 9px 0; gap: 9px; }
    .queue-row .qlabel { line-height: 1.5; }
    .queue-row .qpct { font-size: 12px; }
    .pill { font-size: 11.5px; padding: 2px 6px; }
    .card-head h3 { font-size: 16.5px; }
    .card-head .meta { font-size: 13.5px; }
    .status-text b { font-size: 15.5px; }
    .status-text span { font-size: 13.5px; }
    .island { background: rgba(250, 248, 243, .94); border-color: rgba(255, 255, 255, .85); box-shadow: 0 10px 30px rgba(90, 84, 72, .18); }
    .island .island-text b { color: #4a4741; font-size: 14px; }
    .island .island-text span { color: #8b857a; font-size: 12px; }
    .island .fill { stroke: #8fa39a; }
    .island .track { stroke: #e2ded5; }
    /* ---------- 完成摘要与状态标签改为浅色 ---------- */
    .done-card { background: linear-gradient(160deg, rgba(255, 255, 255, .74), rgba(255, 255, 255, .44)); }
    .done-item { background: rgba(255, 255, 255, .58); border-color: rgba(255, 255, 255, .75); border-radius: 9px; box-shadow: inset 0 1px 0 rgba(255, 255, 255, .8); }
    .done-item span { color: #8b857a; }
    .done-item b { color: #3b3a36; font-size: 16px; }
    .done-title { color: #5f7a6d; }
    .done-card .card-head .meta { color: #8b857a; }
    .pill { background: rgba(147, 167, 157, .16); color: #5f7a6d; border: 1px solid rgba(147, 167, 157, .22); }
    .pill.done { background: rgba(147, 167, 157, .26); color: #3f6b54; border-color: rgba(147, 167, 157, .38); }
    .pill.failed { background: rgba(185, 141, 136, .16); color: #9a5f58; border-color: rgba(185, 141, 136, .28); }
    .pill.paused { background: rgba(185, 162, 122, .18); color: #8a6a2f; border-color: rgba(185, 162, 122, .3); }
    .queue-row .qpct { color: #8b857a; }
  </style>
</head>
<body>
  <div class="island" id="island" aria-live="polite">
    <svg class="ring" viewBox="0 0 36 36" aria-hidden="true">
      <circle class="track" cx="18" cy="18" r="14"></circle>
      <circle class="fill" id="islandRing" cx="18" cy="18" r="14"></circle>
      <path class="check" d="M11.5 18.5l4.4 4.4L25 13.6"></path>
    </svg>
    <div class="island-text"><b id="islandTitle">正在下载</b><span id="islandSub">准备中</span></div>
  </div>

  <div class="app">
    <aside class="sidebar">
      <div class="brand"><div class="logo">JM</div><div><div class="name">JMComic</div><div class="ver">STUDIO 2.7.10</div></div></div>
      <nav class="nav">
        <button type="button" class="active" data-act="task"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg>新建任务</button>
        <button type="button" data-act="search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.2-3.2"></path></svg>搜索作品</button>
        <button type="button" data-act="queue"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"></path></svg>下载队列<span class="count" id="queueCount">0</span></button>
        <button type="button" data-act="log"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M5 5h14v14H5z"></path><path d="M8 9h8M8 13h5"></path></svg>运行日志</button>
        <button type="button" data-act="open"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2h7A1.5 1.5 0 0 1 19 9.5v8A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5z"></path></svg>输出目录</button>
      </nav>
      <div class="side-foot">
        <div class="status-line"><i></i><span id="statusChip" style="color:inherit"><span>就绪</span></span></div>
        <div class="out-label">输出目录</div>
        <div class="out-path" id="sideOutPath">下载</div>
      </div>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <div class="search-shell">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.2-3.2"></path></svg>
          <input id="searchQuery" placeholder="搜索作品名称，回车确认" autocomplete="off"><kbd>Enter</kbd>
        </div>
        <button id="search" class="icon-btn" type="button" aria-label="搜索"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.2-3.2"></path></svg></button>
        <span class="sys"><i style="width:6px;height:6px;border-radius:50%;background:#3fb950;display:inline-block"></i>本地服务</span>
      </header>

      <div class="content">
        <section class="card results-panel">
          <div class="card-head"><h3>搜索结果</h3><span class="meta" id="resultCount">未搜索</span></div>
          <div class="filter-row">
            <div id="searchFilter" class="seg" role="group">
              <button type="button" data-category="all" class="active">全部</button>
              <button type="button" data-category="hanman">韩漫</button>
              <button type="button" data-category="japanese">日漫</button>
            </div>
          </div>
          <div id="searchResults" class="results"><div class="empty-note">输入名称后搜索，或者直接在右侧填入本子 ID。</div></div>
        </section>

        <section class="config">
          <div class="card card-a">
            <div class="card-head"><h3><span class="badge">A</span>任务来源</h3><span class="meta">本子 ID</span></div>
            <div class="card-body">
              <div class="field">
                <label for="albums">作品 ID</label>
                <div class="path-row">
                  <input id="albums" type="text" placeholder="每行一个，例如 1114751">
                  <button id="loadAlbum" class="mini-btn" type="button">搜索作品</button>
                </div>
              </div>
              <div class="row-2" style="margin-top:10px">
                <div class="field">
                  <label for="option">选择文件（可选）</label>
                  <input id="option" type="text" placeholder="option.yml">
                </div>
                <div class="field">
                  <label for="output">输出目录</label>
                  <div class="path-row"><input id="output" type="text" value="下载"><button id="openOutputBtn" class="mini-btn" type="button" title="打开目录">📁</button></div>
                </div>
              </div>
              <div id="albumChips" class="chips"></div>
            </div>
          </div>

          <div class="card card-c">
            <div class="card-head"><h3><span class="badge">C</span>输出策略</h3><span class="meta">格式与压缩</span></div>
            <div class="card-body">
              <div class="field">
                <label>保存格式</label>
                <div id="formatToggle" class="fmt-row">
                  <button type="button" class="fmt active" data-format="jpg">JPG</button>
                  <button type="button" class="fmt" data-format="pdf">PDF</button>
                  <button type="button" class="fmt" data-format="both">JPG + PDF</button>
                </div>
              </div>
              <div class="setting">
                <span class="label">PDF 页面方向</span>
                <div id="orientation" class="mini-seg">
                  <button type="button" data-orient="auto" class="active">自动</button>
                  <button type="button" data-orient="portrait">纵向</button>
                  <button type="button" data-orient="landscape">横向</button>
                </div>
              </div>
              <div class="setting">
                <span class="label">图片质量</span>
                <div style="display:flex;align-items:center;gap:8px">
                  <input id="quality" type="range" min="50" max="100" step="5" value="100">
                  <b id="qualityValue" style="font:11px var(--mono);color:#cfe6ff;width:32px">100%</b>
                </div>
              </div>
              <div class="setting">
                <span class="label">跳过异常图片</span>
                <label class="switch"><input id="skipBad" type="checkbox" checked><i></i></label>
              </div>
              <div class="setting">
                <span class="label">保留原始图片</span>
                <label class="switch"><input id="keepRaw" type="checkbox"><i></i></label>
              </div>
            </div>
          </div>

          <div class="card card-b">
            <div class="card-head"><h3><span class="badge">B</span>章节范围</h3><span class="meta" id="chapterMeta">未加载</span></div>
            <div class="card-body">
              <div class="chapter-toolbar">
                <label class="radio"><input type="radio" name="scope" value="all" checked>全部章节</label>
                <label class="radio"><input type="radio" name="scope" value="custom">自定义章节</label>
                <span class="meta" id="chapterHint">未选择时下载全部章节</span>
              </div>
              <div class="chapter-tools">
                <input id="chapterSearch" type="text" placeholder="搜索章节…">
                <button id="selectAll" class="mini-btn" type="button" disabled>全选</button>
                <button id="selectNone" class="mini-btn" type="button" disabled>清空</button>
              </div>
              <div id="chapterBox" class="chapter-box"></div>
              <textarea id="photos" class="sr-only"></textarea>
            </div>
          </div>

          <div class="card card-d">
            <div class="card-head"><h3><span class="badge">D</span>目录策略</h3><span class="meta">固定规则</span></div>
            <div class="card-body">
              <ul class="policy-list">
                <li>按 API 章节顺序下载</li>
                <li>章节编号自然排序（第 1、2、10 章）</li>
                <li>图片按文件名顺序写入</li>
                <li>PDF 与 Venera 包均按章节顺序生成</li>
              </ul>
            </div>
          </div>

          <div class="action-bar">
            <span class="summary" id="taskSummary">共 <b>0</b> 章 · <b>0</b> 张图片</span>
            <div class="buttons">
              <button id="clearConfig" class="btn btn-ghost" type="button">清空配置</button>
              <button id="preview" class="btn btn-ghost" type="button">预览任务</button>
              <button id="start" class="btn btn-primary" type="button">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h13"></path><path d="m13 6 6 6-6 6"></path></svg>
                <span id="startLabel">开始下载</span>
              </button>
            </div>
          </div>
        </section>

        <aside class="inspector">
          <div class="card album-card" id="albumCard">
            <div class="card-body">
              <div class="album-head">
                <img id="albumCover" alt="">
                <div class="t"><b id="albumTitle">—</b><span id="albumAuthor">—</span><span id="albumIds">—</span></div>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card-head"><h3>任务信息</h3><span class="meta" id="jobState">空闲</span></div>
            <div class="card-body">
              <dl style="margin:0">
                <div class="kv"><dt>本子</dt><dd><b id="albumCount">0</b> 个</dd></div>
                <div class="kv"><dt>章节</dt><dd><b id="photoCount">0</b> 个</dd></div>
                <div class="kv"><dt>图片总数</dt><dd id="infoImages">—</dd></div>
                <div class="kv"><dt>输出格式</dt><dd id="infoFormat">JPG</dd></div>
                <div class="kv"><dt>当前排序</dt><dd>章节索引（固定）</dd></div>
                <div class="kv"><dt>文件名规则</dt><dd>章节_序号_页码.jpg</dd></div>
              </dl>
            </div>
          </div>

          <div class="card">
            <div class="card-head"><h3>任务监控</h3><span class="meta" id="progressText">0%</span></div>
            <div class="card-body">
              <div class="status-body">
                <div class="ring-wrap">
                  <svg viewBox="0 0 66 66" aria-hidden="true"><circle class="ring-track" cx="33" cy="33" r="24"></circle><circle class="ring-fill" id="progressRing" cx="33" cy="33" r="24"></circle></svg>
                  <span class="ring-pct" id="ringPct">0%</span>
                </div>
                <div class="status-text"><b id="statusText">准备就绪</b><span id="statusSub">等待任务输入</span></div>
              </div>
              <div class="bar"><i id="progressBar"></i></div>
              <div style="margin-top:8px">
                <div class="stat-line"><span>章节</span><b id="statChapters">0 / 0</b></div>
                <div class="stat-line"><span>当前图片</span><b id="statImages">0 / 0</b></div>
                <div class="stat-line"><span>当前章节</span><b class="ell" id="statChapterName">—</b></div>
                <div class="stat-line"><span>速度 / 剩余</span><b id="statSpeed">—</b></div>
              </div>
              <div class="monitor-actions">
                <button id="pause" class="btn btn-ghost" type="button" disabled>暂停</button>
                <button id="resume" class="btn btn-ghost" type="button" disabled>继续</button>
                <button id="cancel" class="btn btn-danger" type="button" disabled>取消</button>
              </div>
            </div>
          </div>

          <div class="card" id="queueCard">
            <div class="card-head"><h3>下载队列</h3><span class="meta" id="queueMeta">空</span></div>
            <div class="card-body" id="queueBody"><div class="hint">还没有任务</div></div>
          </div>

          <div class="card done-card" id="doneCard">
            <div class="card-head"><h3>完成摘要</h3><span class="meta">已完成</span></div>
            <div class="card-body">
              <div class="done-title">✓ 已按章节顺序完成整理</div>
              <div class="done-grid">
                <div class="done-item"><span>章节</span><b id="doneChapters">0</b></div>
                <div class="done-item"><span>图片</span><b id="doneImages">0</b></div>
                <div class="done-item"><span>PDF 页数</span><b id="donePdf">0</b></div>
                <div class="done-item"><span>异常图片</span><b id="doneErrors">0</b></div>
              </div>
              <div class="buttons" style="display:flex;gap:6px">
                <button id="doneOpen" class="btn btn-ghost" type="button" style="flex:1;height:30px;font-size:12px">打开输出目录</button>
                <button id="doneAgain" class="btn btn-ghost" type="button" style="flex:1;height:30px;font-size:12px">重新下载</button>
              </div>
            </div>
          </div>
        </aside>

        <section class="card log-card">
          <div class="card-head">
            <h3>运行日志</h3>
            <div class="log-tools">
              <label><input id="autoScroll" type="checkbox" checked>自动滚动</label>
              <button id="copyLog" class="mini-btn" type="button" style="height:26px;font-size:11.5px">复制日志</button>
              <button id="clearLog" class="mini-btn" type="button" style="height:26px;font-size:11.5px">清空</button>
              <span class="meta" id="logCount"></span>
            </div>
          </div>
          <div id="log" class="log-list"><div class="log-row plain"><span class="lm">[UI] 等待任务输入…</span></div></div>
        </section>
      </div>
    </main>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let running = false, paused = false, pollTimer = null, seenLogs = 0;
    let outputFormat = 'jpg', searchCategory = 'all', albumData = null, quality = 100;
    let pdfOrientation = 'auto', skipBad = true, keepRaw = false;
    let islandState = 'idle', hideTimer = null, lastLogs = [];

    const split = (text) => text.trim().split(/[\s,，;；]+/).filter(Boolean);
    const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const fmtLabel = (v) => v === 'pdf' ? 'PDF' : v === 'both' ? 'JPG + PDF' : 'JPG';
    const fmtBytes = (n) => { if (!n) return '0 B'; const u = ['B','KB','MB','GB']; let i = 0, v = n; while (v >= 1024 && i < 3) { v /= 1024; i += 1; } return v.toFixed(i ? 1 : 0) + ' ' + u[i]; };
    const fmtEta = (s) => { if (s == null || s < 0) return '—'; const m = Math.floor(s / 60); return (m ? m + '分' : '') + (s % 60) + '秒'; };

    const refreshCounts = () => {
      const albums = split($('albums').value);
      const photos = split($('photos').value);
      $('albumCount').textContent = albums.length;
      $('photoCount').textContent = photos.length || (albumData ? albumData.chapters : 0);
      const chapterCount = albumData ? (photos.length || albumData.chapters) : photos.length;
      const imageCount = albumData ? albumData.pages : 0;
      $('taskSummary').innerHTML = `共 <b>${chapterCount}</b> 章 · <b>${imageCount.toLocaleString()}</b> 张图片`;
      $('infoImages').textContent = albumData ? albumData.pages.toLocaleString() : '—';
      $('albumChips').innerHTML = albums.slice(0, 6).map((x) => `<span class="chip">JM${escapeHtml(x)}</span>`).join('');
    };

    const setChip = (text, color) => {
      const chip = $('statusChip');
      chip.querySelector('span').textContent = text;
      chip.style.color = color || 'var(--green)';
      chip.classList.toggle('running', text === '下载中');
    };

    const setRunning = (value, isPaused) => {
      running = value; paused = Boolean(isPaused);
      $('start').disabled = value;
      $('cancel').disabled = !value;
      $('pause').disabled = !value || paused;
      $('resume').disabled = !paused;
      $('jobState').textContent = value ? (paused ? '已暂停' : '下载中') : '空闲';
      $('startLabel').textContent = value ? '下载中…' : '开始下载';
    };

    const setProgressUI = (p) => {
      const ratio = p && typeof p.ratio === 'number' ? p.ratio : 0;
      const pct = Math.round(ratio * 100);
      $('progressText').textContent = pct + '%';
      $('ringPct').textContent = pct + '%';
      $('progressBar').style.width = pct + '%';
      const c = 2 * Math.PI * 24;
      $('progressRing').style.strokeDasharray = c;
      $('progressRing').style.strokeDashoffset = c * (1 - pct / 100);
      if (p) {
        $('statChapters').textContent = (p.chapters_done || 0) + ' / ' + (p.chapters_total || 0);
        $('statImages').textContent = (p.images_done || 0) + ' / ' + (p.images_total || 0);
        $('statChapterName').textContent = p.current_chapter || '—';
        $('statSpeed').textContent = (p.speed ? fmtBytes(p.speed) + '/s' : '—') + ' · ' + fmtEta(p.eta);
        $('statusSub').textContent = p.phase || '';
      }
    };

    const islandRun = (p) => {
      const island = $('island');
      if (islandState === 'done') return;
      islandState = 'running';
      island.classList.remove('done', 'fail');
      island.classList.add('show');
      const ratio = p && typeof p.ratio === 'number' ? p.ratio : null;
      const c = 2 * Math.PI * 14;
      $('islandRing').style.strokeDasharray = c;
      if (ratio != null && p && p.images_total) {
        island.classList.remove('spinning');
        $('islandRing').style.strokeDashoffset = c * (1 - ratio);
        $('islandTitle').textContent = '正在下载';
        $('islandSub').textContent = Math.round(ratio * 100) + '% · ' + p.images_done + '/' + p.images_total;
      } else {
        island.classList.add('spinning');
        $('islandTitle').textContent = '正在下载';
        $('islandSub').textContent = '正在准备…';
      }
    };

    const islandPause = () => {
      islandState = 'paused';
      $('island').classList.remove('spinning', 'done', 'fail');
      $('island').classList.add('show');
      $('islandTitle').textContent = '已暂停';
      $('islandSub').textContent = '点击继续后恢复下载';
    };

    const islandDone = (sub) => {
      const island = $('island');
      if (islandState === 'done') return;
      islandState = 'done';
      island.classList.remove('spinning', 'fail');
      island.classList.add('show', 'done');
      $('islandTitle').textContent = '已完成';
      $('islandSub').textContent = sub || '文件已写入输出目录';
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => { island.classList.remove('show'); islandState = 'idle'; }, 3200);
    };

    const islandFail = (title, message) => {
      const island = $('island');
      islandState = 'fail';
      island.classList.remove('spinning', 'done');
      island.classList.add('show', 'fail');
      $('islandTitle').textContent = title || '下载失败';
      $('islandSub').textContent = message || '请查看运行日志';
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => { island.classList.remove('show'); islandState = 'idle'; }, 4200);
    };

    const rowFor = (line) => {
      const match = line.match(/^(\d{2}:\d{2}:\d{2})\s+([A-Z]+)\s+([\s\S]*)$/);
      if (!match) return `<div class="log-row plain"><span class="lm">${escapeHtml(line)}</span></div>`;
      const [, time, level, message] = match;
      const cls = level === 'ERROR' ? 'err' : level === 'WARNING' ? 'warn' : (/(完成|成功)/.test(message) ? 'ok' : '');
      return `<div class="log-row ${cls}"><span class="lt">${time}</span><span class="lv">${level}</span><span class="lm">${escapeHtml(message)}</span></div>`;
    };

    const appendLogs = (logs) => {
      lastLogs = logs;
      if (logs.length < seenLogs) seenLogs = 0;
      if (!logs.length) { $('log').innerHTML = '<div class="log-row plain"><span class="lm">[UI] 等待任务输入…</span></div>'; $('logCount').textContent = ''; return; }
      $('log').innerHTML = logs.map(rowFor).join('');
      $('logCount').textContent = logs.length + ' 条';
      seenLogs = logs.length;
      if ($('autoScroll').checked) $('log').scrollTop = $('log').scrollHeight;
    };

    const renderQueue = (queue) => {
      $('queueCount').textContent = queue.length;
      $('queueMeta').textContent = queue.length ? queue.length + ' 个任务' : '空';
      if (!queue.length) { $('queueBody').innerHTML = '<div class="hint">还没有任务</div>'; return; }
      const cls = { done: 'done', failed: 'failed', paused: 'paused', canceled: 'failed' };
      const label = { queued: '排队', running: '下载中', paused: '已暂停', done: '完成', failed: '失败', canceled: '已取消', canceling: '取消中' };
      $('queueBody').innerHTML = queue.map((t) => `<div class="queue-row"><span class="pill ${cls[t.status] || ''}">${label[t.status] || escapeHtml(t.status)}</span><span class="qlabel">${escapeHtml(t.label)}</span><span class="qpct">${Math.round((t.ratio || 0) * 100)}%</span></div>`).join('');
    };

    const showDone = (stats, status) => {
      if (status !== 'done' || (stats && stats.dry_run)) { $('doneCard').classList.remove('show'); return; }
      $('doneCard').classList.add('show');
      $('doneChapters').textContent = stats.chapters || 0;
      $('doneImages').textContent = stats.images || 0;
      $('donePdf').textContent = stats.pdf_pages || 0;
      $('doneErrors').textContent = stats.errors || 0;
    };

    const poll = async () => {
      try {
        const data = await fetch('/api/status').then((r) => r.json());
        appendLogs(data.logs || []);
        $('statusText').textContent = data.status;
        setProgressUI(data.progress || {});
        renderQueue(data.queue || []);
        setRunning(data.running, data.paused);
        if (data.running) {
          setChip(data.paused ? '已暂停' : '下载中', data.paused ? 'var(--amber)' : '#58a6ff');
          if (data.paused) islandPause(); else islandRun(data.progress || {});
          $('doneCard').classList.remove('show');
        } else {
          if (data.last_status === 'failed') { setChip('出错', 'var(--red)'); islandFail('下载失败', data.status.replace(/^任务失败\s*·?\s*/, '')); }
          else if (data.last_status === 'done') { setChip('就绪', 'var(--green)'); setProgressUI({ ...(data.progress || {}), ratio: 1 }); islandDone((data.stats || {}).dry_run ? '预览完成，未写文件' : ''); }
          else if (data.last_status === 'canceled') { setChip('就绪', 'var(--green)'); islandFail('已取消', '任务已取消'); }
          else setChip('就绪', 'var(--green)');
          showDone(data.stats || {}, data.last_status);
          if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        }
      } catch (e) {
        setChip('离线', 'var(--red)');
        islandFail('连接断开', '本地服务无响应');
      }
    };

    const renderResults = (items) => {
      $('resultCount').textContent = '共 ' + items.length + ' 个';
      $('searchResults').innerHTML = items.map((item) => {
        const meta = [item.author, item.chapters ? item.chapters + ' 章' : '', item.pages ? item.pages.toLocaleString() + ' 页' : ''].filter(Boolean).join(' · ');
        return `<button class="result-item" type="button" data-id="${escapeHtml(item.id)}"><img class="cover" loading="lazy" src="${escapeHtml(item.cover)}" alt="" onerror="this.style.display='none'"><span class="result-main"><span class="result-title">${escapeHtml(item.title || '未命名')}</span><span class="result-meta">${escapeHtml(meta || ('JM' + item.id))}</span></span><span class="result-arrow">›</span></button>`;
      }).join('');
      $('searchResults').querySelectorAll('.result-item').forEach((node) => node.addEventListener('click', () => {
        $('albums').value = node.dataset.id;
        refreshCounts();
        loadAlbum();
      }));
    };

    const searchByName = async () => {
      const query = $('searchQuery').value.trim();
      if (!query) { $('searchResults').innerHTML = '<div class="empty-note">请输入本子名称。</div>'; return; }
      $('search').disabled = true;
      $('resultCount').textContent = '搜索中…';
      $('searchResults').innerHTML = '<div class="empty-note">正在搜索并获取详情，稍等几秒…</div>';
      try {
        const response = await fetch('/api/search', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({query, category: searchCategory, option: $('option').value.trim()})});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '搜索失败');
        if (!data.results.length) { $('searchResults').innerHTML = '<div class="empty-note">没有找到匹配结果。</div>'; $('resultCount').textContent = '0 个'; return; }
        renderResults(data.results);
      } catch (error) {
        $('searchResults').innerHTML = `<div class="empty-note">${escapeHtml(error.message)}</div>`;
        $('resultCount').textContent = '搜索失败';
      } finally { $('search').disabled = false; }
    };

    const renderAlbum = (data) => {
      albumData = data;
      $('albumCard').classList.add('show');
      $('albumCover').src = data.cover;
      $('albumTitle').textContent = data.title;
      $('albumAuthor').textContent = '作者：' + (data.author || '未知');
      $('albumIds').textContent = `ID ${data.id} · ${data.chapters} 章 · ${data.pages.toLocaleString()} 页`;
      $('chapterMeta').textContent = data.chapters + ' 章 · ' + data.pages.toLocaleString() + ' 页';
      $('selectAll').disabled = false;
      $('selectNone').disabled = false;
      renderChapters(data.episodes);
      refreshCounts();
    };

    const renderChapters = (episodes) => {
      $('chapterBox').innerHTML = episodes.map((ep) => `<label class="chapter-row" data-name="${escapeHtml((ep.name || '').toLowerCase())}"><input type="checkbox" value="${escapeHtml(ep.id)}"><span class="idx">${escapeHtml(ep.index)}</span><span class="name">${escapeHtml(ep.name || '未命名')}</span></label>`).join('');
      $('chapterBox').querySelectorAll('input').forEach((box) => box.addEventListener('change', () => {
        if (box.checked) document.querySelector('input[name="scope"][value="custom"]').checked = true;
        syncSelection();
      }));
    };

    const syncSelection = () => {
      const custom = document.querySelector('input[name="scope"]:checked').value === 'custom';
      const ids = custom ? [...$('chapterBox').querySelectorAll('input:checked')].map((b) => b.value) : [];
      $('photos').value = ids.map((id) => 'p' + id).join('\n');
      $('chapterHint').textContent = custom ? (ids.length ? '已选 ' + ids.length + ' 个章节' : '未选择章节') : '将下载全部章节';
      refreshCounts();
    };

    const loadAlbum = async () => {
      const albumId = split($('albums').value).find((x) => /^\d+$/.test(x));
      if (!albumId) { $('chapterMeta').textContent = '未加载'; $('chapterBox').innerHTML = '<div class="chapter-empty">先在作品 ID 里填入本子编号</div>'; return; }
      $('chapterMeta').textContent = '加载中…';
      try {
        const data = await fetch('/api/album?id=' + encodeURIComponent(albumId)).then((r) => r.json());
        if (data.error) throw new Error(data.error);
        renderAlbum(data);
      } catch (error) {
        $('chapterMeta').textContent = '加载失败';
        $('chapterBox').innerHTML = `<div class="chapter-empty">${escapeHtml(error.message)}</div>`;
      }
    };

    const openFolder = async () => {
      const path = $('output').value.trim() || '下载';
      try {
        const response = await fetch('/api/open-folder', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path})});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '无法打开目录');
      } catch (error) {
        $('log').innerHTML = `<div class="log-row err"><span class="lt">--:--:--</span><span class="lv">ERROR</span><span class="lm">${escapeHtml(error.message)}</span></div>`;
      }
    };

    const postAction = async (path) => fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}).then((r) => r.json());

    const submit = async (isPreview) => {
      if (running) return;
      const albums = $('albums').value, photos = $('photos').value;
      if (!split(albums).length && !split(photos).length) {
        $('log').innerHTML = '<div class="log-row warn"><span class="lt">--:--:--</span><span class="lv">WARNING</span><span class="lm">请至少填写一个本子或章节。</span></div>';
        return;
      }
      const payload = { albums, photos, option: $('option').value.trim(), output: $('output').value.trim(), format: outputFormat, dry_run: Boolean(isPreview), quality, pdf_orientation: pdfOrientation, skip_bad: skipBad, keep_raw: keepRaw };
      const response = await fetch('/api/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
      const data = await response.json();
      if (!response.ok) {
        $('log').innerHTML = `<div class="log-row err"><span class="lt">--:--:--</span><span class="lv">ERROR</span><span class="lm">${escapeHtml(data.error)}</span></div>`;
        return;
      }
      islandState = 'idle';
      seenLogs = 0;
      $('doneCard').classList.remove('show');
      setRunning(true, false);
      islandRun(null);
      poll();
      if (!pollTimer) pollTimer = setInterval(poll, 1500);
    };

    $('albums').addEventListener('input', refreshCounts);
    $('output').addEventListener('input', () => { $('sideOutPath').textContent = $('output').value.trim() || '下载'; });
    $('loadAlbum').addEventListener('click', loadAlbum);
    $('albums').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadAlbum(); });
    $('selectAll').addEventListener('click', () => { $('chapterBox').querySelectorAll('input').forEach((b) => { b.checked = true; }); document.querySelector('input[name="scope"][value="custom"]').checked = true; syncSelection(); });
    $('selectNone').addEventListener('click', () => { $('chapterBox').querySelectorAll('input').forEach((b) => { b.checked = false; }); syncSelection(); });
    document.querySelectorAll('input[name="scope"]').forEach((radio) => radio.addEventListener('change', syncSelection));
    $('chapterSearch').addEventListener('input', () => {
      const term = $('chapterSearch').value.trim().toLowerCase();
      $('chapterBox').querySelectorAll('.chapter-row').forEach((row) => { row.style.display = !term || row.dataset.name.includes(term) || row.textContent.toLowerCase().includes(term) ? '' : 'none'; });
    });
    document.querySelectorAll('#formatToggle button').forEach((button) => button.addEventListener('click', () => {
      outputFormat = button.dataset.format;
      document.querySelectorAll('#formatToggle button').forEach((other) => other.classList.toggle('active', other === button));
      $('infoFormat').textContent = fmtLabel(outputFormat);
    }));
    document.querySelectorAll('#orientation button').forEach((button) => button.addEventListener('click', () => {
      pdfOrientation = button.dataset.orient;
      document.querySelectorAll('#orientation button').forEach((other) => other.classList.toggle('active', other === button));
    }));
    document.querySelectorAll('#searchFilter button').forEach((button) => button.addEventListener('click', () => {
      searchCategory = button.dataset.category;
      document.querySelectorAll('#searchFilter button').forEach((other) => other.classList.toggle('active', other === button));
    }));
    $('quality').addEventListener('input', () => { quality = Number($('quality').value); $('qualityValue').textContent = quality + '%'; });
    $('skipBad').addEventListener('change', () => { skipBad = $('skipBad').checked; });
    $('keepRaw').addEventListener('change', () => { keepRaw = $('keepRaw').checked; });
    $('search').addEventListener('click', searchByName);
    $('searchQuery').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchByName(); });
    $('start').addEventListener('click', () => submit(false));
    $('preview').addEventListener('click', () => submit(true));
    $('pause').addEventListener('click', () => postAction('/api/pause').then(poll));
    $('resume').addEventListener('click', () => postAction('/api/resume').then(poll));
    $('cancel').addEventListener('click', () => postAction('/api/cancel').then(poll));
    $('clearConfig').addEventListener('click', () => {
      $('albums').value = ''; $('photos').value = ''; $('option').value = ''; $('output').value = '下载';
      $('chapterBox').innerHTML = ''; $('chapterMeta').textContent = '未加载'; $('chapterHint').textContent = '未选择时下载全部章节';
      $('albumCard').classList.remove('show'); albumData = null;
      document.querySelector('input[name="scope"][value="all"]').checked = true;
      outputFormat = 'jpg'; document.querySelectorAll('#formatToggle button').forEach((b) => b.classList.toggle('active', b.dataset.format === 'jpg'));
      $('infoFormat').textContent = 'JPG';
      refreshCounts();
    });
    $('clearLog').addEventListener('click', () => { $('log').innerHTML = ''; seenLogs = 0; lastLogs = []; $('logCount').textContent = ''; });
    $('copyLog').addEventListener('click', async () => { try { await navigator.clipboard.writeText(lastLogs.join('\n')); } catch (e) { /* ignore */ } });
    $('openOutputBtn').addEventListener('click', openFolder);
    $('doneOpen').addEventListener('click', openFolder);
    $('doneAgain').addEventListener('click', () => submit(false));
    $('autoScroll').addEventListener('change', () => { if ($('autoScroll').checked) $('log').scrollTop = $('log').scrollHeight; });

    document.querySelectorAll('.nav button').forEach((button) => button.addEventListener('click', () => {
      const act = button.dataset.act;
      document.querySelectorAll('.nav button').forEach((other) => other.classList.toggle('active', other === button));
      if (act === 'search') { $('searchQuery').focus(); return; }
      if (act === 'log') { $('log').scrollIntoView({behavior: 'smooth', block: 'center'}); return; }
      if (act === 'queue') { $('queueCard').scrollIntoView({behavior: 'smooth', block: 'center'}); return; }
      if (act === 'open') { openFolder(); return; }
      $('albums').focus();
    }));

    document.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); $('searchQuery').focus(); }
    });

    if (location.hash === '#demo-running') islandRun({ratio: 0.42, images_total: 3900, images_done: 1638});
    if (location.hash === '#demo-done') { islandDone(); clearTimeout(hideTimer); }

    refreshCounts();
    setProgressUI({ ratio: 0 });
    poll();
  </script>
</body>
</html>'''


def _parse_ids(text: str) -> tuple[list[str], list[str]]:
    from .jm_toolkit import JmcomicText

    albums: list[str] = []
    photos: list[str] = []
    for token in (item for item in re.split(r'[\s,，;；]+', text.strip()) if item):
        is_photo = token.lower().startswith('p')
        value = token[1:] if is_photo else token
        try:
            parsed = JmcomicText.parse_to_jm_id(value)
        except Exception as error:
            raise ValueError(f'无法解析 ID: {value}') from error
        (photos if is_photo else albums).append(parsed)
    return albums, photos


class _QueueLogHandler(logging.Handler):
    def __init__(self, events: queue.Queue):
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put(self.format(record))
        except Exception:
            self.handleError(record)


_IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.jpe', '.png', '.webp', '.gif', '.avif'}


def _natural_sort_key(value: str | Path) -> tuple[Any, ...]:
    """Sort chapter/page names like a reader expects: 2 before 10."""
    parts = re.split(r'(\d+)', str(value).casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _safe_venera_name(value: str, fallback: str) -> str:
    """Keep titles usable as Windows directory names without changing display text."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or '').strip())
    name = name.rstrip(' .')
    return name[:150] or fallback


def _download_result_list(result: Any) -> list[Any]:
    if result is None:
        return []
    succeeded = getattr(result, 'succeeded', None)
    if succeeded is not None:
        return list(succeeded)
    return [result]


def _export_venera(results: list[Any], output_dir: str) -> tuple[int, int]:
    """Create one Venera-compatible ZIP archive per downloaded comic."""
    if not results:
        return 0, 0
    export_root = Path(output_dir).resolve().parent / 'Venera-导入'
    export_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    comics: dict[str, dict[str, Any]] = {}

    for result in results:
        detail = getattr(result, 'detail', None)
        album = getattr(result, 'album', None)
        if detail is None or album is None:
            continue
        manifest = getattr(result, 'manifest', None)
        image_paths = list(getattr(manifest, 'image_filepath_list', []) or [])
        if not image_paths:
            continue

        comic_name = _safe_venera_name(getattr(album, 'name', ''), f'JM{album.id}')
        comic_key = str(getattr(album, 'id', comic_name))
        comic = comics.setdefault(
            comic_key,
            {
                'name': comic_name,
                'staging': export_root / f'.{comic_name}-{comic_key}.staging',
                'chapters': {},
            },
        )
        staging_dir: Path = comic['staging']
        staging_dir.mkdir(parents=True, exist_ok=True)

        chapter_dirs: dict[str, Path] = {}
        for image_path in image_paths:
            source = Path(image_path)
            if source.suffix.lower() not in _IMAGE_SUFFIXES or not source.is_file():
                continue
            source_parent = str(source.parent.resolve())
            chapter_dir = chapter_dirs.get(source_parent) or comic['chapters'].get(source_parent)
            if chapter_dir is None:
                chapter_name = _safe_venera_name(source.parent.name, '未命名章节')
                chapter_dir = staging_dir / chapter_name
                chapter_dirs[source_parent] = chapter_dir
                comic['chapters'][source_parent] = chapter_dir
                chapter_dir.mkdir(parents=True, exist_ok=True)
            destination = chapter_dir / source.name
            if source.resolve() == destination.resolve():
                continue
            shutil.copy2(source, destination)
            copied += 1

    for comic in comics.values():
        staging_dir: Path = comic['staging']
        if not staging_dir.is_dir():
            continue
        archive_path = export_root / f"[JM{_safe_venera_name(str(comic['name']), 'comic')}].zip"
        temp_archive = export_root / f'.{archive_path.name}.tmp'
        if temp_archive.exists():
            temp_archive.unlink()
        with zipfile.ZipFile(temp_archive, 'w', compression=zipfile.ZIP_STORED) as archive:
            for file_path in sorted(
                    staging_dir.rglob('*'),
                    key=lambda path: _natural_sort_key(path.relative_to(staging_dir))):
                if file_path.is_file() and file_path.suffix.lower() in _IMAGE_SUFFIXES:
                    archive.write(file_path, Path(comic['name']) / file_path.relative_to(staging_dir))
        os.replace(temp_archive, archive_path)
        shutil.rmtree(staging_dir)

    # The archive is the durable output. Remove only files recorded by this
    # download task, leaving unrelated historical files in the output folder.
    cleanup_paths: set[Path] = set()
    for result in results:
        manifest = getattr(result, 'manifest', None)
        if manifest is None:
            continue
        cleanup_paths.update(Path(path) for path in getattr(manifest, 'image_filepath_list', []) or [])
        for paths in getattr(manifest, 'export_filepath_dict', {}).values():
            cleanup_paths.update(Path(path) for path in paths or [])
    cleanup_dirs: set[Path] = set()
    for path in cleanup_paths:
        try:
            if path.is_file():
                cleanup_dirs.add(path.parent)
                path.unlink()
        except OSError:
            continue
    for directory in sorted(cleanup_dirs, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass

    return len(comics), copied


JOB = JobManager()


class _Handler(BaseHTTPRequestHandler):
    server_version = 'JMComicStudio/1.0'

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def _send(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        self._send(json.dumps(value, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8', status)

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get('Content-Length', '0') or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in ('/', '/index.html'):
            self._send(HTML.encode('utf-8'), 'text/html; charset=utf-8')
            return
        if path == '/api/status':
            self._json(JOB.snapshot())
            return
        if path == '/api/album':
            album_id = (query.get('id') or [''])[0]
            try:
                self._json(JOB.album(album_id))
            except ValueError as error:
                self._json({'error': str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001
                self._json({'error': f'获取本子信息失败：{error}'}, HTTPStatus.BAD_GATEWAY)
            return
        if path == '/api/cover':
            album_id = (query.get('id') or [''])[0]
            try:
                self._send(JOB.cover(album_id), 'image/jpeg', HTTPStatus.OK)
            except Exception as error:  # noqa: BLE001
                self._json({'error': str(error)}, HTTPStatus.NOT_FOUND)
            return
        self._json({'error': 'Not found'}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        payload = self._payload()
        if path == '/api/search':
            try:
                results = JOB.search(
                    str(payload.get('query', '')),
                    str(payload.get('option', '')).strip(),
                    str(payload.get('category', 'all')).strip().lower(),
                )
                self._json({'results': results})
            except (ValueError, RuntimeError) as error:
                self._json({'error': str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001
                self._json({'error': f'搜索失败：{error}'}, HTTPStatus.BAD_GATEWAY)
            return
        if path == '/api/start':
            try:
                JOB.submit(payload)
                self._json(JOB.snapshot())
            except (ValueError, RuntimeError) as error:
                self._json({'error': str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:  # noqa: BLE001
                self._json({'error': f'启动失败：{error}'}, HTTPStatus.BAD_REQUEST)
            return
        if path in ('/api/pause', '/api/resume', '/api/cancel'):
            task_id = str(payload.get('id', ''))
            action = {'/api/pause': JOB.pause, '/api/resume': JOB.resume, '/api/cancel': JOB.cancel}[path]
            action(task_id)
            self._json(JOB.snapshot())
            return
        if path == '/api/open-folder':
            try:
                target = str(payload.get('path', '')).strip() or '.'
                folder = Path(target).expanduser()
                if not folder.is_absolute():
                    folder = Path.cwd() / folder
                folder = folder.resolve()
                folder.mkdir(parents=True, exist_ok=True)
                if hasattr(os, 'startfile'):
                    os.startfile(str(folder))
                else:
                    import subprocess
                    subprocess.Popen(['xdg-open', str(folder)])
                self._json({'ok': True, 'path': str(folder)})
            except Exception as error:  # noqa: BLE001
                self._json({'error': f'打开目录失败：{error}'}, HTTPStatus.BAD_REQUEST)
            return
        self._json({'error': 'Not found'}, HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description='JMComic browser UI')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    url = f'http://{args.host}:{args.port}/'
    print(f'JMComic Studio: {url}')
    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()











