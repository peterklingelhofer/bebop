//! Smoke tests for PyO3 ↔ bebop integration.
//! These run with `cargo test`. They actually init a Python interpreter
//! and import the bebop package — if either fails, all later phases will
//! fail too, so it's worth catching here.

use bebop_rs::python;

#[test]
fn embedded_python_initializes_and_imports_bebop() {
    let v = python::bebop_version().expect("import bebop + read package metadata");
    // we don't pin the exact version (it changes); we just want a
    // well-formed semver-ish string back
    assert!(
        !v.is_empty() && v.chars().any(|c| c.is_ascii_digit()),
        "expected a version string, got {v:?}"
    );
    eprintln!("bebop version: {v}");
}

#[test]
fn embedded_python_can_call_rhythm_module() {
    use pyo3::prelude::*;
    Python::with_gil(|py| {
        python::ensure_venv_on_path(py).expect("venv on sys.path");
        // call bebop.rhythm.all_rhythm_names() and verify charleston is there
        let rhythm = py.import_bound("bebop.rhythm").expect("import bebop.rhythm");
        let names: Vec<String> = rhythm
            .getattr("all_rhythm_names")
            .unwrap()
            .call0()
            .unwrap()
            .extract()
            .unwrap();
        assert!(
            names.iter().any(|n| n == "charleston"),
            "expected 'charleston' in rhythm names, got {names:?}"
        );
        eprintln!("{} rhythms available, including charleston", names.len());
    });
}
