//! Compiles and runs `tests/ffi_smoke.c` against the bebop-rs static lib,
//! exercising `bebop_init`, `bebop_process_audio`, and `bebop_destroy`.
//!
//! This is the canonical "the C ABI actually works" check — the C++ AU
//! shell uses the same .a file, so if this test passes, the AU's link
//! step will succeed too.

use std::path::Path;
use std::process::Command;

#[test]
fn c_can_link_and_call_ffi() {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let lib_dir = manifest.join("target/release");
    let staticlib = lib_dir.join("libbebop_rs.a");
    assert!(
        staticlib.exists(),
        "missing {} — run `cargo build --release` first",
        staticlib.display()
    );

    // Resolve libpython from the same uv-managed Python PyO3 linked
    // against at build time. Easier than env-var juggling: ask Python
    // directly via the venv interpreter.
    let py = manifest.join("../.venv/bin/python3");
    let prefix = Command::new(&py)
        .args(["-c", "import sys; print(sys.base_prefix)"])
        .output()
        .expect("ran python3 from .venv")
        .stdout;
    let py_lib_dir = format!(
        "{}/lib",
        String::from_utf8_lossy(&prefix).trim()
    );

    let out = lib_dir.join("ffi_smoke_c");
    let rpath_arg = format!("-Wl,-rpath,{}", py_lib_dir);
    let status = Command::new("clang")
        .arg("-o").arg(&out)
        .arg(manifest.join("tests/ffi_smoke.c"))
        .arg("-I").arg(manifest.join("include"))
        .arg("-L").arg(&lib_dir)
        .arg("-lbebop_rs")
        .arg("-L").arg(&py_lib_dir)
        .arg("-lpython3.12")
        .arg(&rpath_arg)
        .arg("-framework").arg("CoreFoundation")
        .arg("-framework").arg("Security")
        .arg("-framework").arg("SystemConfiguration")
        .status()
        .expect("clang");
    assert!(status.success(), "clang failed to link C smoke test");

    let output = Command::new(&out).output().expect("run smoke test");
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    eprintln!("--- ffi_smoke stdout ---\n{}", stdout);
    if !stderr.is_empty() {
        eprintln!("--- ffi_smoke stderr ---\n{}", stderr);
    }
    assert!(
        output.status.success(),
        "C smoke test exited with {:?}",
        output.status
    );
    assert!(stdout.contains("PASS"), "expected PASS in stdout");
}
