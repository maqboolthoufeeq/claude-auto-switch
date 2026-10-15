#!/usr/bin/env node
'use strict';
// cswitch: run Switchboard (the Python app bundled in this package) with uv, or with a plain Python venv.
const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const PKG = path.resolve(__dirname, '..');
const VERSION = require(path.join(PKG, 'package.json')).version;
const HOME = path.resolve(process.env.CSWITCH_HOME || path.join(os.homedir(), '.cswitch'));
const WIN = process.platform === 'win32';

// command -> [script, replacement command]. Anything else runs the dashboard launcher.
const COMMANDS = {
  dashboard: ['rotator_ui.py'],
  init: ['rotator.py', 'init'],
  login: ['rotator.py', 'login'],
  accounts: ['rotator.py', 'status'],
  run: ['rotator.py', 'run'],
  resume: ['rotator.py', 'resume'],
  setup: ['shared_mode.py', 'setup'],
  enable: ['shared_mode.py', 'enable'],
  disable: ['shared_mode.py', 'disable'],
  status: ['shared_mode.py', 'status'],
  switch: ['shared_mode.py', 'switch'],
  fallback: ['shared_mode.py', 'fallback'],
  migrate: ['shared_mode.py', 'migrate'],
  watch: ['shared_mode.py', 'watch'],
  hook: ['shared_mode.py', 'hook'],
  'ensure-monitor': ['shared_mode.py', 'ensure-monitor'],
};

const USAGE = `cswitch ${VERSION} - switch between Claude Code accounts automatically

Usage:
  cswitch                      open the local dashboard (starts it if needed)
  cswitch dashboard [--no-browser]
  cswitch login <account>      sign in one profile through Claude's browser login
  cswitch accounts             show each profile's login status
  cswitch setup                prepare signed-in profiles for shared switching
  cswitch enable | disable     turn automatic switching on or off
  cswitch status               show the shared-login state as JSON
  cswitch switch <account>     change the shared Claude login now
  cswitch fallback on|off      switch at a reported usage limit too
  cswitch run --project <dir> "<task>"   run a guided task across profiles ("-" reads the task from stdin)
  cswitch resume --project <dir>         continue the saved guided task

Data (config, profiles, state) lives in ${HOME}  (override with CSWITCH_HOME).
Requires Claude Code and either uv (https://docs.astral.sh/uv/) or Python 3.9+.`;

function works(cmd, args) {
  const result = spawnSync(cmd, args, { stdio: 'ignore', windowsHide: true });
  return !result.error && result.status === 0;
}

function findUv() {
  const home = os.homedir();
  const candidates = ['uv', path.join(home, '.local', 'bin', WIN ? 'uv.exe' : 'uv'),
    path.join(home, '.cargo', 'bin', WIN ? 'uv.exe' : 'uv'), '/opt/homebrew/bin/uv', '/usr/local/bin/uv'];
  return candidates.find((candidate) => works(candidate, ['--version'])) || null;
}

function findPython() {
  const probe = 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)';
  const candidates = WIN ? [['py', ['-3']], ['python', []], ['python3', []]] : [['python3', []], ['python', []]];
  return candidates.find(([cmd, pre]) => works(cmd, [...pre, '-c', probe])) || null;
}

function ensureVenv([cmd, pre]) {
  const venv = path.join(HOME, 'venv-pip');
  const python = WIN ? path.join(venv, 'Scripts', 'python.exe') : path.join(venv, 'bin', 'python');
  const stamp = path.join(venv, 'cswitch-deps.txt');
  const deps = ['pyte>=0.8.2,<0.9'];
  if (WIN || process.platform === 'linux') deps.push('keyring>=25.6,<26');
  if (WIN) deps.push('pywinpty>=2.0.15,<4');
  const wanted = [VERSION, ...deps].join('\n');
  let current = '';
  try { current = fs.readFileSync(stamp, 'utf8'); } catch (_) { /* first run */ }
  if (fs.existsSync(python) && current === wanted) return python;
  console.error(`cswitch: preparing a Python environment in ${venv} (one time)`);
  const steps = [
    [cmd, [...pre, '-m', 'venv', venv], 'Could not create a Python virtual environment. Install uv, or on Debian/Ubuntu run: sudo apt install python3-venv'],
    [python, ['-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', ...deps], 'Could not install Python dependencies. Check your network connection, or install uv instead.'],
  ];
  for (const [stepCmd, args, failure] of steps) {
    const result = spawnSync(stepCmd, args, { stdio: 'inherit', windowsHide: true });
    if (result.error || result.status !== 0) {
      console.error(`cswitch: ${failure}`);
      process.exit(1);
    }
  }
  fs.writeFileSync(stamp, wanted);
  return python;
}

function main(argv) {
  if (argv[0] === '--version' || argv[0] === '-v') { console.log(VERSION); return 0; }
  if (argv[0] === '--help' || argv[0] === '-h' || argv[0] === 'help') { console.log(USAGE); return 0; }
  let script = 'launch.py';
  let args = argv;
  if (argv.length && !argv[0].startsWith('-')) {
    const entry = COMMANDS[argv[0]];
    if (!entry) { console.error(`cswitch: unknown command "${argv[0]}"\n\n${USAGE}`); return 2; }
    script = entry[0];
    args = [...entry.slice(1), ...argv.slice(1)];
  }
  fs.mkdirSync(HOME, { recursive: true, mode: 0o700 });
  const env = { ...process.env, CSWITCH_HOME: HOME, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8' };
  let cmd;
  let cmdArgs;
  const uv = findUv();
  if (uv) {
    delete env.VIRTUAL_ENV; // an activated venv elsewhere is not this app's environment
    env.UV_PROJECT_ENVIRONMENT = path.join(HOME, 'venv');
    cmd = uv;
    cmdArgs = ['run', '--project', PKG, '--frozen', '--no-dev', 'python', path.join(PKG, script), ...args];
  } else {
    const python = findPython();
    if (!python) {
      console.error('cswitch: Python 3.9+ or uv is required. Install uv from https://docs.astral.sh/uv/ and run cswitch again.');
      return 1;
    }
    cmd = ensureVenv(python);
    cmdArgs = [path.join(PKG, script), ...args];
  }
  process.on('SIGINT', () => { /* let the Python process handle Ctrl+C and exit first */ });
  const result = spawnSync(cmd, cmdArgs, { stdio: 'inherit', env });
  if (result.error) { console.error(`cswitch: ${result.error.message}`); return 1; }
  return result.status === null ? 1 : result.status;
}

process.exit(main(process.argv.slice(2)));
