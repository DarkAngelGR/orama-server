# Fresh-install storage choices in the browser wizard

Date: 2026-11-17
Status: Approved for implementation

## Intent and agreed scope

The owner wants storage choices in the browser wizard for fresh installations only.
Users choose where app settings/databases and the combined media/download tree live.
Existing installation migration, Docker image storage relocation, and independent
per-app or separate movie/download storage roots are excluded.

## Approaches

Installer prompts are simpler but the owner chose browser configuration.
The selected approach is a two-stage bootstrap: the installer establishes narrowly
bounded storage options, then the browser selects them before apps are deployed.
Arbitrary host browsing and mounting whole host volumes into the running Agent are
excluded. The browser never supplies new host mount authority.

## Proposed user experience

The Folders step offers recommended defaults or custom storage. It has two selectors:

- App settings and databases: choose an available storage location.
- Media and downloads: choose an available storage location.

Both may use the same drive, in distinct directories. Show exact resulting paths,
available space when recently measured, and the fixed internal folder tree. Do not
infer SSD/HDD labels from a path or promise current free space from installer data.
The final Review step shows these choices before deployment. Setup can reconnect
following a restart; no fixed restart duration is promised. Once activation begins,
locations are locked, including direct Agent requests. Retrying the same interrupted
activation is supported; choosing a different layout after activation is not.

Owner-approved scope: users may customize folder names within installer-authorized Orama areas. Provide defaults (apps and data) and preview full resulting paths. Each name is a single path component, not an absolute path or nested path. Both web and Agent validate names, rejecting separators, dot names, traversal, control characters, colons, reserved names, excessive length and overlapping targets. Use a documented conservative naming grammar. Keep the internal app and media/download structure fixed. Refuse existing foreign targets rather than adopting or changing ownership of user data.

## Storage domains

Keep the existing config root as the control location for Orama state, secrets,
compose files, and root-owned activation records. Do not relocate the socket or
claim that it is stored beneath the config root.

Add app_config_root, defaulting to config_root/config. Each app receives only
app_config_root/<app>, consistent with its pinned mount target. data_root holds
the existing unified media/ and downloads/ layout and remains mounted as /data.
App data and media roots must not overlap each other or sensitive control paths.
The default config_root/config relationship is an explicit compatibility exception;
arbitrary nesting under the control root is not permitted.

## Trusted storage preparation

Discovery runs in the host installer context, not a container that merely sees its
own mount table. Finding a volume does not authorize access to all of its contents.
Bootstrap may prepare dedicated, fixed Orama directories on supported storage
volumes. It must refuse existing foreign directories, symlink components, unsafe
ancestors, system paths, and unexpected nested mounts. It must not recursively
change ownership of existing user data. Recognize directories from an interrupted
bootstrap only through trusted installation identity/state.

For a single-drive Linux host, explicitly authorize existing bootstrap defaults;
never authorize or mount / just because /data resides on the root filesystem.
Unrecognized or unavailable volumes remain unavailable options. Extra locations
must be established by the host installer, never by a web-supplied absolute path.
Creating small empty dedicated directories during bootstrap is a proposed tradeoff;
leave unused ones intact rather than automatically deleting directories.

Record opaque candidate IDs, exact prepared paths, installation identity, and mount
identity information in a root-owned manifest under the control root, outside the
web-mounted state directory. Protect the containing directory against replacement.
The web gets a sanitized projection through the Agent socket. Requests carry only
candidate IDs and validated folder names; the Agent derives exact paths from its authoritative manifest and the approved naming grammar.
Never use string-prefix tests or a web-owned copy as authorization.

## Validation and preparation helper

The current Agent cannot inspect unmounted host locations. A proposed short-lived
probe helper mounts only the exact already-prepared candidate directories; it gets
no Docker socket, no network, no privileged mode, and no general host-volume mount.
Use a pinned image/entrypoint and fixed arguments. Run write probes as the configured
app identity; bootstrap must already have established appropriate permissions.
Drop capabilities, use a read-only root filesystem, and bound execution/output.
Do not enable Docker's implicit creation of missing source directories.

For custom names, the bounded helper creates child folders only within the exact installer-prepared Orama area, as the configured app identity. Use no-follow directory descriptors, exclusive creation and installation-owned retry records. No extra capabilities or arbitrary chown are allowed. Refuse foreign target collisions. Verify candidate mount identity/availability before use; a missing NAS disk must
not silently fall back to writing onto the system drive. Paths must have trusted
ancestors and no-follow validation. Exact host-to-helper mount binding, resistance
to substitution between validation and Docker mounting, and supported mount identity
checks require a concrete implementation plan and security proof before coding.
If this cannot be achieved without broader authority, stop and revise the design.
Do not silently mount an entire volume as a workaround.

Probe hardlink support using temporary files in the actual proposed downloads and
media directories under the app identity, then remove only those probe files.
st_dev alone is not proof. Report success as capability at validation time, not a
promise of instant imports for all future operations. Retain the binding release
spec's unified-filesystem requirement; incompatible layouts block activation rather
than silently introducing cross-filesystem operation.

## Activation and restart recovery

A root-owned journal records transaction ID, manifest version, selected IDs, validated
compose digest, and phases: prepared, activating, core-ready, deployed, or recovery-required.
The Agent serializes activation and freezes the selection before deployment side effects.
A missing marker on an existing installation never means permission to reconfigure:
only an explicit trusted fresh-install bootstrap state grants this capability.

The Agent builds both app and core compose documents from pinned templates; it never
accepts a browser-supplied core document or helper command. Validate builders and Agent
pins together. Keep the existing web container mount isolation and networkless Agent.

Where core recreation is required, retain the last verified compose document and
stage the new one separately. Use the existing self-update helper pattern only after
checking its actual permissions and validating every additional mount or command.
Do not grant a new helper blanket Docker authority by analogy to self-update.

The transaction coordinator must survive core recreation, use bounded health checks,
verify the Agent's selected roots/transaction identity, and restore the previous core
configuration if activation fails. A durable journal and a host restart recovery path
must cover power loss, not just errors caught by a running helper. Atomic file rename
alone is insufficient. If rollback also fails, retain both configurations and provide
specific installer recovery instructions without deleting or moving app/media data.
Retries of the same transaction are idempotent. No apps start before the expected core
configuration is verified. Browser authentication remains in its original state directory;
reconnection and expired-session behavior are tested rather than assumed.

Re-running the installer preserves the trusted activation record and selected roots.
It does not unlock locations or act as a migration mechanism.

## Compatibility and affected areas

Review install.sh, src/orama/cli.py, setup_model.py, layout.py, compose.py, the wizard
forms/templates, wiring paths, and src/orama_agent validation/server/runner code.
Audit all callers of app_config_dir and all default path assumptions. Old saved setups
without app_config_root retain config_root/config. Existing installs get read-only
storage settings, including after upgrades. Headless/bootstrap defaults continue to work.

## Required proof before release

- Reject forged IDs, altered web projections, traversal, prefix collisions, symlinks,
  overlapping roots, foreign nonempty directories, swapped/missing mounts, and replay.
- Prove only trusted fresh installs can activate; test concurrent and repeated requests.
- Verify exact helper mounts, image, entrypoint, capabilities, network and socket absence.
- Test interrupted activation and rollback at every durable phase, including host reboot
  and partial Docker recreation. Preserve previous authentication and configuration.
- Exercise single-drive defaults and separate app/media storage in real Linux VM tests.
- Test actual hardlinks, permission failures, disk-full errors, and unavailable storage.
- Browser checks cover keyboard operation, errors, path previews, reconnect and locked state.
- Run repository-required tests, Ruff, ShellCheck, hygiene, Docker and VM proofs.
- Security review is required for the new Agent commands, pins, bootstrap authority and
  recovery mechanism; the proposal is not a claim that these guarantees are implemented.

## Review disposition and remaining choice

The first draft was rejected as implementation-ready: it trusted a web-writable manifest,
assumed access to unmounted paths, lacked transactional recovery, and overstated hardlink
and restart guarantees. This revision removes those assumptions and makes the required
proof explicit. It also removes personal absolute links before tracking the document.

Owner decision: allow custom folder names within an installer-authorized dedicated Orama area. Arbitrary existing host folders remain outside this fresh-install proposal. Product scope is settled; the design awaits owner approval before implementation planning. Concrete host mount identity checks and restart recovery remain mandatory engineering and security gates, not established guarantees.
