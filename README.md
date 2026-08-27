# nodoom.composer

Compose Nodoom posts from the Omarchy bar.

![Nodoom composer opened from the Omarchy bar, draft already filled](preview.png)

Adapted from [bitr0t.omarchytweet](https://github.com/rmacy/omarchytweet) — same job queue, same private redirect handoff, same draft persistence. Nodoom has no public write API, so this plugin is **browser handoff only**.

## Posting mode

**Browser composer (the only mode, free)** — opens [nodoom.app/composer](https://nodoom.app/composer) with your draft in `?text=`, so Nodoom prefills *What's on your mind?*. The draft is also copied to the clipboard as a backup. You press the final **Post** button in your browser. No API keys needed.

Stay logged in. 24-hour vs permanent expiry is chosen in Nodoom's own composer after the handoff.

## Install

```sh
omarchy plugin add https://github.com/maiosx/omarchynodoom.git --enable
```

Then restart the shell if the bar icon does not appear: `omarchy restart shell`.

Add a Hyprland binding to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + N", "Nodoom Composer", "omarchy-shell nodoom.composer toggle")
```

Choose any unused chord if `SUPER + X` is already bound.

From a local checkout:

```sh
omarchy plugin add ./omarchynodoom --enable
```

## Configure

```sh
mkdir -m 700 -p ~/.config/npost
cp ~/.config/omarchy/plugins/nodoom.composer/config.example.toml ~/.config/npost/config.toml
chmod 600 ~/.config/npost/config.toml
```

Edit `~/.config/npost/config.toml` to set `copy_draft`. The backend also generates this template with correct permissions on first run.

## Controls

- Click the **N** icon in the bar to open a fullscreen composer overlay.
- **Escape** or **Close** dismisses. Enter inserts a newline.
- The action button is always *Continue in Nodoom*.
- Drafts persist across panel open/close cycles and are shared across monitors.
- Posts are capped at 500 characters. The cap is
  enforced in the panel, at the QML IPC boundary, and again in the backend.

## IPC

The plugin exposes two `omarchy-shell` targets.

### Overlay

```sh
omarchy-shell nodoom.composer toggle
omarchy-shell nodoom.composer open
omarchy-shell nodoom.composer close
```

`show` and `hide` are aliases for `open` and `close`.

### Prefill

```sh
omarchy-shell nodoom.composer.compose compose "hello"
```

Opens the overlay and stages the text. The handler returns one of:

| Result | Meaning |
| --- | --- |
| `ok` | Draft accepted (empty text is a no-op) |
| `pending-replace` | A draft already exists; confirm **Replace** or **Keep** in the overlay. Incoming text is not persisted until you confirm. |
| `too-long` | Over 500 characters; rejected before the service stores or persists it |
| `busy` | A handoff is already in progress |
| `not-ready` | Composer service is still starting |
| `service-unavailable` | Bar widget / service did not load |

Text is a single string argument. Do not put post text on the shell command line from untrusted input.

## Optional CLI

The `bin/npost` wrapper resolves the backend relative to itself. Symlink it into your PATH:

```sh
ln -sf ~/.config/omarchy/plugins/nodoom.composer/bin/npost ~/.local/bin/npost
```

Post text is always passed on **stdin**, never as a command-line argument:

```sh
printf '%s' 'Your post text here' | npost post
```

Other commands (`mode`, `enqueue`, `status`, `active`, `ack`, `draft`) pass through directly to the backend.

Exit codes: **0** composer opened; **1** failed / busy / unknown; **2** usage error.

## File architecture

```
backend.py              Posting backend (Python 3.11+ stdlib only)
Service.qml             Singleton service: backend IPC, draft state, job queue
Panel.qml               Per-monitor composer UI
manifest.json           Omarchy plugin metadata
config.example.toml     Configuration template
nodoom.png              Bar icon (symbolic, tinted to the bar foreground)
preview.png             Marketplace preview (Omarchy bar + Nodoom composer)
bin/npost               Optional CLI wrapper (resolves backend relative to itself)
tests/test_backend.py   Isolated stdlib backend regression suite
pyproject.toml          Poetry development metadata (runtime has no dependencies)
LICENSE                 MIT license
```

Runtime state (job files, locks) lives under `$XDG_RUNTIME_DIR/nodoom.composer`. Drafts live in `~/.config/npost` (0700 dir, 0600 files).

## How the handoff stays off `/proc`

`xdg-open` exposes its argument through process metadata. The worker writes an owner-only local HTML redirect (`intent.html` inside the job directory) and passes **only that file URI** to `xdg-open`. The Nodoom composer URL (`/composer?text=…`) never appears in argv.

Clipboard copy itself is stdin to `wl-copy` / `xclip` / `xsel`, never argv.

## Development

```sh
omarchy plugin validate .
python3 tests/test_backend.py
```

Runtime never depends on Poetry.

## Security

- `~/.config/npost` is created with mode 0700; `config.toml` with 0600. Symlinks, foreign owners, and group/other permission bits are refused.
- Secrets are never logged, echoed, or included in JSON output (this plugin has none).
- The handoff URL embeds draft text as `?text=` so Nodoom can prefill the composer, and is never returned in JSON or logs.
- Post text arrives via stdin or job files — never via shell interpolation or command-line arguments.
- The public `nodoom.composer.compose` IPC handler rejects text over 500 characters before the shared service stores or persists it. An IPC prefill never overwrites an existing draft until the user confirms in the panel.

## Uninstall

```
omarchy plugin remove nodoom.composer
```

Remove the optional CLI symlink and configuration if desired:

```sh
rm -f ~/.local/bin/npost
rm -rf ~/.config/npost
```

## License

MIT — see [LICENSE](LICENSE). Adapted from bitr0t.omarchytweet by Ryan Macy.
