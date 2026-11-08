//! The fixed paths the Python scripts hardcode (scripts/notify.py, mc_panel.py, worlds.py, mc_agent.py), in one
//! place. The values are product constants that Ansible also knows (`ansible/group_vars/all/main.yml`); none of
//! them may change without the Python side changing too. Only the server directory has an override
//! (`$MC_SERVER_DIR`, mc_rcon.py `SERVER_DIR`: see mcrcon.rs).

/// notify.py `STATE_DIR` (= mc_state_dir): the ops user writes, everyone reads.
pub const STATE_DIR: &str = "/var/lib/minecraft";
/// notify.py `MAINTENANCE_FILE`: health.py's self-heal respects an open window.
pub const MAINTENANCE_FILE: &str = "/var/lib/minecraft/maintenance.json";
/// notify.py `ADMIN_STOPPED_FILE`: the server was stopped on purpose.
pub const ADMIN_STOPPED_FILE: &str = "/var/lib/minecraft/admin-stopped";
/// notify.py `RESTORE_LOCK_FILE`: restore.py holds an flock on it while it replaces the world.
pub const RESTORE_LOCK_FILE: &str = "/var/lib/minecraft/restore.lock";
/// notify.py `FENCED_FILE`: `mcctl migrate` fenced this box.
pub const FENCED_FILE: &str = "/var/lib/minecraft/fenced";
/// notify.py `SNAPSHOTS_FILE`: restic's snapshot list (backup.py --list-snapshots).
pub const SNAPSHOTS_FILE: &str = "/var/lib/mc-snapshots/snapshots.json";
/// worlds.py `WORLDS_FILE`: archives + the active world (mc-world.service, mc-backup.service).
pub const WORLDS_FILE: &str = "/var/lib/mc-snapshots/worlds.json";
/// worlds.py `LIBRARY_FILE` (= mc_worlds_library_file): the deploy's world library (Git + pending).
pub const WORLDS_LIBRARY_FILE: &str = "/opt/minecraft/worlds-library.json";
/// worlds.py `REQUEST_FILE`: written by the agent, read by mc-world.service.
pub const WORLD_REQUEST_FILE: &str = "/var/lib/minecraft/world-request.json";
/// mc_panel.py `PLUGINS_DISABLED_FILE`.
pub const PLUGINS_DISABLED_FILE: &str = "/var/lib/minecraft/plugins-disabled.json";
/// mc_panel.py `PLUGINS_LOCK_FILE` (= mc_plugins_lock_json, plugins role).
pub const PLUGINS_LOCK_FILE: &str = "/opt/minecraft/plugins-lock.json";
/// mc_panel.py `JOB_LOCK_FILE`: one job at a time across processes (mc-agent and `cli`).
pub const JOB_LOCK_FILE: &str = "/var/lib/minecraft/job.lock";
/// mc_agent.py `PRODUCT_VERSION_FILE` (= mc_product_version_file).
pub const PRODUCT_VERSION_FILE: &str = "/opt/minecraft/product-version.json";
/// mc_agent.py's default state directory (`Settings.state_dir`, `cli_main`'s `state_dir`).
pub const AGENT_STATE_DIR: &str = "/var/lib/mc-agent";
/// mc_agent.py `JOURNAL_SOCKET`: journald's native protocol socket (audit.rs).
pub const JOURNAL_SOCKET: &str = "/run/systemd/journal/socket";
/// mc_agent.py `Settings.client_request_dir` default (/modpack code requests from mc-brand).
pub const CLIENT_REQUEST_DIR: &str = "/run/mc-telemetry/client";
/// mc_agent.py `Settings.client_code_file` default (answers for mc-brand).
pub const CLIENT_CODE_FILE: &str = "/run/mc-verdicts/client-codes.tsv";

/// systemd units the agent starts or watches (mc_agent.py / mc_panel.py / worlds.py constants).
pub mod units {
    /// mc_agent.py `UNIT`, mc_panel.py `UNIT`.
    pub const MINECRAFT: &str = "minecraft.service";
    /// mc_agent.py `BACKUP_UNIT`.
    pub const BACKUP: &str = "mc-backup.service";
    /// mc_panel.py `PLUGINS_APPLY_UNIT`.
    pub const PLUGINS_APPLY: &str = "mc-plugins-apply.service";
    /// worlds.py `PANEL_UNIT`.
    pub const WORLD: &str = "mc-world.service";
}
