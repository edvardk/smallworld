"""P16 performance knobs (design §8 "P16"): the JVM flag sets per jvm.gc, the CPU split, the ZGC-only THP
setting, the overlays' new keys and the bench that compares the collectors. Every default is the behaviour
before P16."""

from __future__ import annotations

import re
import sys

import jinja2
import pytest
import yaml
from conftest import REPO

ROLE = yaml.safe_load((REPO / "ansible/roles/minecraft/defaults/main.yml").read_text())
G1, ZGC = ROLE["mc_jvm_flags_g1"], ROLE["mc_jvm_flags_zgc"]
GC_LOG = "/run/mc-telemetry/gc.log"
# The flags minecraft.service ran with before P16 (frozen): jvm.gc g1 must keep them byte for byte.
PRE_P16 = [
    "-XX:+UseG1GC",
    "-XX:+ParallelRefProcEnabled",
    "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions",
    "-XX:+DisableExplicitGC",
    "-XX:+AlwaysPreTouch",
    "-XX:G1NewSizePercent=30",
    "-XX:G1MaxNewSizePercent=40",
    "-XX:G1HeapRegionSize=8M",
    "-XX:G1ReservePercent=20",
    "-XX:G1HeapWastePercent=5",
    "-XX:G1MixedGCCountTarget=4",
    "-XX:InitiatingHeapOccupancyPercent=15",
    "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:G1RSetUpdatingPauseTimePercent=5",
    "-XX:SurvivorRatio=32",
    "-XX:+PerfDisableSharedMem",
    "-XX:MaxTenuringThreshold=1",
    "-XX:+UseTransparentHugePages",
    f"-Xlog:gc:file={GC_LOG}:time,uptime,level,tags:filecount=3,filesize=5M",
    "-Dusing.aikars.flags=https://mcflags.emc.gs",
    "-Daikars.new.flags=true",
]
# Flags that only mean something to G1 (or to the Parallel collector): never with ZGC.
G1_ONLY = re.compile(
    r"^-XX:(\+UseG1GC|G1|MaxGCPauseMillis|InitiatingHeapOccupancyPercent|SurvivorRatio|MaxTenuringThreshold|"
    r"\+ParallelRefProcEnabled|\+UnlockExperimentalVMOptions)"
)


def ansible_env() -> jinja2.Environment:
    # Ansible templates with trim_blocks on (a block tag's own newline is dropped).
    return jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)  # noqa: S701 - not HTML


def resolve(expr: str, **variables: object) -> object:
    env = ansible_env()
    return env.from_string(expr).render(**variables)


def test_g1_is_the_default_and_unchanged() -> None:
    assert G1 == PRE_P16
    pick = ROLE["mc_jvm_flags"]
    flags = {"mc_jvm_flags_g1": ["g1"], "mc_jvm_flags_zgc": ["zgc"]}
    assert resolve(pick, mc_jvm_gc="g1", **flags) == "['g1']"
    assert resolve(pick, mc_jvm_gc="zgc", **flags) == "['zgc']"


def test_zgc_flags() -> None:
    assert ZGC[0] == "-XX:+UseZGC"
    # Generational is the only ZGC mode on 25: -XX:+ZGenerational was removed in 24 (the JVM prints "Ignoring
    # option ZGenerational; support was removed in 24.0"), so it must not be passed.
    assert not [f for f in ZGC if "ZGenerational" in f]
    assert not [f for f in ZGC if G1_ONLY.match(f)]
    assert not [f for f in ZGC if f.startswith("-Daikars") or f.startswith("-Dusing.aikars")]
    for kept in ("-XX:+AlwaysPreTouch", "-XX:+UseTransparentHugePages", "-XX:+DisableExplicitGC"):
        assert kept in ZGC
    assert not [
        f for f in ZGC if f.startswith(("-Xms", "-Xmx", "-XX:SoftMaxHeapSize"))
    ]  # the unit sets Xms=Xmx
    # Same tmpfs file and rotation as G1; gc+phases carries ZGC's pauses (health.py parses both formats).
    (log,) = [f for f in ZGC if f.startswith("-Xlog:")]
    assert log == f"-Xlog:gc,gc+phases:file={GC_LOG}:time,uptime,level,tags:filecount=3,filesize=5M"
    assert len(set(ZGC)) == len(ZGC)


@pytest.mark.parametrize(
    ("affinity", "vcpus", "want"), [("off", 4, ""), ("split", 4, "1-3"), ("split", 8, "1-7")]
)
def test_jvm_cpus_from_the_host(affinity: str, vcpus: int, want: str) -> None:
    got = resolve(ROLE["mc_jvm_cpus"], mc_cpu_affinity=affinity, ansible_facts={"processor_vcpus": vcpus})
    assert got == want


def test_split_is_refused_below_three_cpus_before_any_role() -> None:
    """The assert is a site.yml pre_task: base and backups apply mc_helper_cpus (AllowedCPUs=0 on mc-backup and
    mc-prune) and THP/noatime long before the minecraft role, so a refusal there would leave a half-applied split."""
    (play,) = yaml.safe_load((REPO / "ansible/site.yml").read_text())
    (task,) = [t for t in play["pre_tasks"] if t.get("name") == "CPU affinity split needs at least 3 CPUs"]
    assert task["when"] == "mc_cpu_affinity == 'split'"
    assert task["ansible.builtin.assert"]["that"] == "(ansible_facts['processor_vcpus'] | int) >= 3"
    assert task["tags"] == ["always"]  # also with --tags minecraft / --tags backups
    assert play.get("gather_facts", True) is not False  # processor_vcpus comes from the play's fact gathering
    for role in ("minecraft", "base", "backups"):
        tasks = yaml.safe_load((REPO / f"ansible/roles/{role}/tasks/main.yml").read_text())
        assert not [t for t in tasks if "processor_vcpus" in str(t.get("ansible.builtin.assert", ""))]


def _thp_content(gc: str) -> str:
    tasks = yaml.safe_load((REPO / "ansible/roles/base/tasks/main.yml").read_text())
    (task,) = [t for t in tasks if t.get("name") == "Transparent Huge Pages = madvise at boot"]
    return str(resolve(task["ansible.builtin.copy"]["content"], mc_jvm_gc=gc))


def test_thp_shmem_advise_only_for_zgc() -> None:
    before = (
        "# Managed by Ansible (base role)\n"
        "w /sys/kernel/mm/transparent_hugepage/enabled - - - - madvise\n"
        "w /sys/kernel/mm/transparent_hugepage/defrag - - - - defer+madvise\n"
    )
    assert _thp_content("g1") == before  # byte for byte what P7..P15 wrote: no change, no handler
    assert (
        _thp_content("zgc") == before + "w /sys/kernel/mm/transparent_hugepage/shmem_enabled - - - - advise\n"
    )


def test_overlays_carry_the_knobs() -> None:
    props = (REPO / "server-config/server.properties.yml.j2").read_text()
    assert "network-compression-threshold: {{ mc_network_compression_threshold }}" in props
    paper = yaml.safe_load(
        jinja2.Environment(undefined=jinja2.StrictUndefined)  # noqa: S701 - not HTML
        .from_string((REPO / "server-config/paper-global.yml.j2").read_text())
        .render(mc_chunk_worker_threads=-1)
    )
    assert paper["chunk-system"] == {"worker-threads": -1}  # Paper's own default (pinned paper-global)
    keys = set((REPO / "server-config/keys/26.3/paper-global.txt").read_text().split())
    assert "chunk-system.worker-threads" in keys and "chunk-system.io-threads" in keys


def test_noatime_task_is_opt_in_and_never_adds_entries() -> None:
    tasks = yaml.safe_load((REPO / "ansible/roles/base/tasks/main.yml").read_text())
    (task,) = [t for t in tasks if t.get("name") == "Mount the server's filesystem with noatime"]
    assert task["when"] == "mc_noatime | bool" and task["mc_noatime"] == {"path": "{{ mc_server_dir }}"}


def test_bench_gc_uses_the_product_flags(tmp_path) -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    import bench_gc

    for gc, flags in (("g1", G1), ("zgc", ZGC)):
        got = bench_gc.jvm_flags(gc, str(tmp_path / "gc.log"))
        assert got == [f.replace(GC_LOG, str(tmp_path / "gc.log")) for f in flags]
    assert bench_gc.taskset_prefix("split", 4) == ["taskset", "-c", "1-3"]
    assert bench_gc.taskset_prefix("off", 4) == []
    with pytest.raises(SystemExit):
        bench_gc.taskset_prefix("split", 2)


def test_ci_runs_the_collector_ab_and_the_p16_deploy() -> None:
    wf = yaml.safe_load((REPO / ".github/workflows/e2e-mock.yml").read_text())
    bench = "\n".join(s.get("run", "") for s in wf["jobs"]["bench-gc"]["steps"])
    assert "scripts/bench_gc.py" in bench and "--affinity" in bench and "--rounds 2" in bench
    assert "shmem_enabled" in bench  # THP as the base role sets it for zgc
    uses = [s.get("uses", "") for s in wf["jobs"]["bench-gc"]["steps"]]
    assert any(u.startswith("actions/upload-artifact@") for u in uses)
    e2e = "\n".join(s.get("run", "") for s in wf["jobs"]["e2e"]["steps"])
    assert "-e @/src/product/tests/e2e/p16.yml" in e2e and "tests/e2e/perf_check.py" in e2e
    assert 'changed=0 +unreachable=0 +failed=0\' "$RUNNER_TEMP/site-p16-2.log"' in e2e
    assert 'changed=0 +unreachable=0 +failed=1\' "$RUNNER_TEMP/site-p16-refused.log"' in e2e  # pre_tasks
    assert "tests/e2e/noatime_check.py /src/product" in e2e
    assert "-e mc_jvm_gc=g1" in e2e and "Revert shmem THP to never" in e2e
    p16 = yaml.safe_load((REPO / "tests/e2e/p16.yml").read_text())
    assert p16["mc_jvm_gc"] == "zgc" and p16["mc_cpu_affinity"] == "split" and p16["mc_helper_cpus"] == "0"
    assert p16["mc_noatime"] is True


def _base_tasks() -> dict:
    tasks = yaml.safe_load((REPO / "ansible/roles/base/tasks/main.yml").read_text())
    return {t.get("name"): t for t in tasks}


def _when(task: dict, **variables: object) -> bool:
    got = ansible_env().from_string("{{ " + task["when"] + " }}").render(**variables)
    assert got in ("True", "False"), got
    return got == "True"


THP_OK = ["always [madvise] never", "defer [defer+madvise]"]


def test_thp_is_applied_from_the_live_values_not_a_handler() -> None:
    """A handler is lost when a later task fails in the same run (the e2e saw shmem_enabled stay `never` after a
    refused split); the role compares sysfs instead, every run."""
    by_name = _base_tasks()
    assert "notify" not in by_name["Transparent Huge Pages = madvise at boot"]
    apply = by_name["Apply the THP settings now"]
    for gc, live, shmem, want in (
        ("g1", THP_OK, "[never] advise", False),
        ("g1", ["[always] madvise never", "defer [defer+madvise]"], "[never] advise", True),
        ("zgc", THP_OK, "[never] advise", True),
        ("zgc", THP_OK, "never [advise]", False),
        ("zgc", ["[always] madvise never", "defer [defer+madvise]"], None, True),
        ("zgc", THP_OK, None, False),  # no shmem_enabled: nothing to apply for it
    ):
        got = _when(
            apply,
            mc_jvm_gc=gc,
            base_thp_live={"stdout_lines": live},
            base_thp_shmem_file={"stat": {"exists": shmem is not None}},
            # a skipped read registers no stdout: the when must not touch it then
            base_thp_shmem_live={"stdout": shmem} if shmem is not None else {"skipped": True},
        )
        assert got is want, (gc, live, shmem)


def test_thp_reads_never_fail_without_shmem_enabled() -> None:
    by_name = _base_tasks()
    read = by_name["Read the live THP settings"]
    assert (
        "shmem_enabled" not in read["ansible.builtin.command"]
    )  # a missing file would fail the whole deploy
    assert read["check_mode"] is False
    stat = by_name["Is shmem THP configurable?"]
    assert stat["ansible.builtin.stat"]["path"] == "/sys/kernel/mm/transparent_hugepage/shmem_enabled"
    shmem = by_name["Read the live shmem THP setting"]
    assert shmem["when"] == "base_thp_shmem_file.stat.exists" and shmem["check_mode"] is False
    names = list(by_name)
    assert names.index("Is shmem THP configurable?") < names.index("Read the live shmem THP setting")
    report = by_name["Report a kernel without shmem THP (jvm.gc zgc)"]
    assert _when(report, mc_jvm_gc="zgc", base_thp_shmem_file={"stat": {"exists": False}})
    assert not _when(report, mc_jvm_gc="g1", base_thp_shmem_file={"stat": {"exists": False}})


@pytest.mark.parametrize(
    ("gc", "shmem", "want"),
    [
        ("g1", "never [advise]", True),  # zgc set it: back to the kernel's default now
        ("g1", "[never] advise", False),  # already reverted: idempotent
        ("g1", "always within_size advise [never] deny force", False),
        ("g1", "[always] within_size advise never deny force", False),  # not ours: left alone
        ("g1", None, False),
        ("zgc", "never [advise]", False),
    ],
)
def test_shmem_advise_is_reverted_live_for_g1(gc: str, shmem: str | None, want: bool) -> None:
    task = _base_tasks()["Revert shmem THP to never (jvm.gc g1)"]
    assert task["ansible.builtin.command"] == "tee /sys/kernel/mm/transparent_hugepage/shmem_enabled"
    assert task["args"] == {"stdin": "never"}
    got = _when(
        task,
        mc_jvm_gc=gc,
        base_thp_shmem_file={"stat": {"exists": shmem is not None}},
        base_thp_shmem_live={"stdout": shmem} if shmem is not None else {"skipped": True},
    )
    assert got is want


def test_bench_profile_renders_the_products_overlays() -> None:
    """scripts/bench_profile.py renders the same templates with mcctl render's variables (plain Jinja2 with the
    Ansible filters they use), so the A/B measures exactly what a deploy would write."""
    import bench_profile

    v = bench_profile.profile_vars("vanilla")
    spigot = bench_profile.render_overlay("spigot.yml.j2", v)["world-settings"]["default"]
    assert spigot["max-tnt-per-tick"] == 0 and spigot["entity-activation-range"]["animals"] == 0
    paper = bench_profile.render_overlay(
        "paper-world-defaults.yml.j2", bench_profile.profile_vars("balanced")
    )
    assert paper["misc"]["redstone-implementation"] == "ALTERNATE_CURRENT"
    assert paper["environment"]["treasure-maps"]["find-already-discovered"]["loot-tables"] == "true"
    props = bench_profile.render_overlay("server.properties.yml.j2", v)
    assert (props["view-distance"], props["simulation-distance"]) == (10, 8)
