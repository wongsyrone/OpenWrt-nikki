"""Run with Python 3, ucode and Mike Farah's yq on PATH."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.tmp = Path(self.scratch.name)
        self.env = dict(os.environ, TEMP_DIR=str(self.tmp / "tmp"),
                        SUBSCRIPTIONS_DIR=str(self.tmp / "subscriptions"))
        Path(self.env["SUBSCRIPTIONS_DIR"]).mkdir()

    def test_rpc_arguments_are_literal(self):
        self.assertIsNotNone(shutil.which("ucode"), "ucode must be on PATH")
        binary = self.tmp / "bin"
        binary.mkdir()
        curl = binary / "curl"
        curl.write_text(
            "#!/usr/bin/python3\nimport json, os, sys\n"
            "with open(os.environ['ARGS_FILE'], 'w') as f: json.dump(sys.argv[1:], f)\n"
            "print('{}')\n"
        )
        curl.chmod(0o755)
        marker = self.tmp / "injected"
        payload = f"'$(touch {marker})`touch {marker}`'\n"
        profile = {"external-controller": "127.0.0.1:9090", "secret": payload}
        source = (ROOT / "luci-app-nikki/root/usr/share/rpcd/ucode/luci.nikki").read_text()
        source = source.replace(
            "import { get_users, get_groups, get_cgroups, load_profile } "
            "from '/etc/nikki/ucode/include.uc';",
            "function get_users() {} function get_groups() {} function get_cgroups() {}\n"
            f"function load_profile() {{ return {json.dumps(profile)}; }}",
        )
        for args, expected in (
            ({"method": "PATCH", "path": "/configs", "query": payload, "body": payload},
             ["--url-query", payload, "--data", payload]),
            ({"method": "GET", "path": "/configs"}, []),
        ):
            script = self.tmp / "rpc.uc"
            script.write_text(source.replace(
                "return { 'luci.nikki': methods };",
                f"methods.api.call({{args: {json.dumps(args)}}});",
            ))
            argv_file = self.tmp / "argv.json"
            result = subprocess.run(["ucode", str(script)], capture_output=True, text=True,
                                    env=dict(self.env, PATH=f"{binary}:{self.env['PATH']}",
                                             ARGS_FILE=str(argv_file)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(argv_file.read_text()), [
                "--proto-default", "http", "--insecure", "--request", args["method"],
                "--oauth2-bearer", payload, *expected, "127.0.0.1:9090/configs",
            ])
            self.assertFalse(marker.exists())
        service = binary / "service"
        service.write_text(f"#!/bin/sh\ntouch {marker}\n")
        service.chmod(0o755)
        for section, expected in ((payload, False), ("../config", False), ("cfg123_ABC", True)):
            script.write_text(source.replace(
                "return { 'luci.nikki': methods };",
                f"printf('%J', methods.update_subscription.call({{args: {{section_id: {json.dumps(section)}}}}}));",
            ))
            result = subprocess.run(["ucode", str(script)], capture_output=True, text=True,
                                    env=dict(self.env, PATH=f"{binary}:{self.env['PATH']}"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["success"], expected)
            self.assertEqual(marker.exists(), expected)

    def subscription(self, content, **extra):
        self.assertIsNotNone(shutil.which("yq"), "Mike Farah's yq must be on PATH")
        fixture = self.tmp / "fixture.yaml"
        fixture.write_text(content)
        cached = Path(self.env["SUBSCRIPTIONS_DIR"]) / "cfg123.yaml"
        cached.write_text("old working subscription\n")
        state = self.tmp / "state"
        state.write_text("")
        source = (ROOT / "nikki/files/nikki.init").read_text().split("update_subscription() {", 1)[1]
        script = self.tmp / "subscription.sh"
        script.write_text(r"""
config_load() { :; }
config_get() {
    case "$3" in
        name) subscription_name=test ;;
        info_url) subscription_info_url= ;;
        url) subscription_url=http://fixture.invalid ;;
        user_agent) subscription_user_agent=test ;;
    esac
}
log() { :; }
uci_remove() { printf 'removed:%s\n' "$3" >> "$STATE"; }
uci_set() { printf '%s=%s\n' "$3" "$4" >> "$STATE"; }
uci_commit() { :; }
mktemp() { [ "${TEMP_FAIL:-0}" = 0 ] && command mktemp "$@"; }
mv() { [ "${MOVE_FAIL:-0}" = 0 ] && command mv "$@"; }
curl() {
    [ "${CURL_FAIL:-0}" = 0 ] || return 22
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -D) : > "$2"; shift ;;
            -o) cp "$FIXTURE" "$2"; shift ;;
        esac
        shift
    done
}
update_subscription() {
""" + source + "\nupdate_subscription cfg123\n")
        result = subprocess.run(["sh", str(script)], capture_output=True, text=True,
                                env=dict(self.env, STATE=str(state), FIXTURE=str(fixture), **extra))
        return result, cached, state.read_text() if state.exists() else ""

    def test_invalid_subscriptions_preserve_cache_and_usage(self):
        for content in ("proxies: null\n", "proxies: []\n", "proxy-providers: {}\n",
                        "not: [valid YAML", "proxies: wrong-type\n"):
            with self.subTest(content=content):
                result, cached, state = self.subscription(content)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(cached.read_text(), "old working subscription\n")
                self.assertNotIn("removed:", state)
                self.assertEqual(list(Path(self.env["TEMP_DIR"]).iterdir()), [])
                self.assertIn("success=0", state)
                self.assertEqual(list(Path(self.env["TEMP_DIR"]).iterdir()), [])

    def test_valid_subscription_and_file_mode(self):
        for content in ("proxies:\n  - name: test\n    type: direct\n",
                        "proxy-providers:\n  test:\n    type: file\n    path: test.yaml\n"):
            with self.subTest(content=content):
                result, cached, state = self.subscription(content)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(cached.read_text(), content)
                self.assertEqual(cached.stat().st_mode & 0o777, 0o644)
                self.assertIn("success=1", state)
                self.assertEqual(list(Path(self.env["TEMP_DIR"]).iterdir()), [])

    def test_download_and_move_failures_preserve_cache(self):
        for failure in ("CURL_FAIL", "MOVE_FAIL", "TEMP_FAIL"):
            with self.subTest(failure=failure):
                result, cached, state = self.subscription("proxies: [{name: test}]\n", **{failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(cached.read_text(), "old working subscription\n")
                self.assertNotIn("removed:", state)

    def hotplug(self, **extra):
        functions = self.tmp / "lib/functions.sh"
        functions.parent.mkdir(exist_ok=True)
        functions.write_text(r"""
config_load() { :; }
config_get_bool() { config_get "$@"; }
config_get() {
    local value
    case "$3" in
        enabled) value=1 ;;
        core_only) value=0 ;;
        tcp_mode|udp_mode) value="${MODE:-tun}" ;;
        ipv4_proxy|ipv6_proxy) value=1 ;;
        tun_timeout) value=2 ;;
        tun_interval) value=1 ;;
        tun_route_table) value=81 ;;
        *) value= ;;
    esac
    export "$1=$value"
}
ip() {
    printf '%s\n' "$*" >> "$IP_LOG"
    case "$*" in
        *"rule list"*) [ "${HAS_RULES:-1}" = 0 ] || echo rule ;;
        *"route replace"*) [ "${ROUTE_FAIL:-0}" = 0 ] ;;
    esac
}
yq() { echo nikki; }
jsonfilter() {
    if [ "${STOP_DURING_WAIT:-0}" = 1 ]; then
        rm -f "$STARTED_FLAG_PATH"
    fi
    return 0
}
log() { printf '%s\n' "$*" >> "$APP_LOG"; }
""")
        include = self.tmp / "etc/nikki/scripts/include.sh"
        include.parent.mkdir(parents=True, exist_ok=True)
        include.write_text("")
        started = self.tmp / "started.flag"
        started.touch()
        ip_log = self.tmp / "ip.log"
        app_log = self.tmp / "app.log"
        ip_log.write_text("")
        app_log.write_text("")
        source = (ROOT / "nikki/files/hotplug/net").read_text()
        source, tail = source.rsplit("exit 0", 1)
        self.assertEqual(tail.strip(), "")
        script = self.tmp / "hotplug.sh"
        script.write_text(source + "wait\n")
        environment = dict(self.env, ACTION="add", INTERFACE="nikki",
                           IPKG_INSTROOT=str(self.tmp), STARTED_FLAG_PATH=str(started),
                           RUN_PROFILE_PATH="fixture.yaml", IP_LOG=str(ip_log),
                           APP_LOG=str(app_log))
        environment.update(extra)
        result = subprocess.run(["sh", str(script)], capture_output=True, text=True,
                                env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (ip_log.read_text() if ip_log.exists() else "",
                app_log.read_text() if app_log.exists() else "")

    def test_tun_routes_are_restored(self):
        routes, log = self.hotplug()
        self.assertIn("-4 route replace unicast default dev nikki table 81", routes)
        self.assertIn("-6 route replace unicast default dev nikki table 81", routes)
        self.assertIn("routes restored", log)

    def test_unrelated_devices_and_absent_rules_are_ignored(self):
        for values in ({"INTERFACE": "eth0"}, {"MODE": "tproxy"}, {"HAS_RULES": "0"}):
            with self.subTest(values=values):
                routes, _ = self.hotplug(**values)
                self.assertNotIn("route replace", routes)

    def test_shutdown_during_wait_does_not_restore_routes(self):
        routes, _ = self.hotplug(STOP_DURING_WAIT="1")
        self.assertNotIn("route replace", routes)

    def test_route_failure_is_not_reported_as_success(self):
        _, log = self.hotplug(ROUTE_FAIL="1")
        self.assertNotIn("routes restored", log)

    def test_selection_cache_is_preserved(self):
        self.assertIn("/etc/nikki/run/cache.db",
                      (ROOT / "nikki/files/nikki.upgrade").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
