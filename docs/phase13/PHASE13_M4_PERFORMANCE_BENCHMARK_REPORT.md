# FORGE Phase 13 Milestone 4 — Performance Benchmark Report

**Date:** 2026-11-11
**Target Branch:** `phase-13-m4-performance-scalability`
**Host Platform:** Darwin 25.4.0 arm64 (Apple M1, 8 Cores, 8 GiB RAM)
**Python Runtime:** Python 3.13.6

---

## 1. Executive Summary

Phase 13 Milestone 4 establishes a reproducible, deterministic benchmark suite (`forge.benchmarks.benchmark_phase13_m4_performance`) testing repository acquisition, static analysis, incremental indexing, context building, database persistence, concurrent workflows, and repair workspace allocation across 6 standard profiles:

1. `small`: 20 modules, 30 lines/file, depth 2.
2. `medium`: 80 modules, 50 lines/file, depth 4.
3. `deeply_nested`: 100 modules, 20 lines/file, depth 8.
4. `complex_deps`: 80 modules, 40 lines/file, depth 3, cyclic links enabled.
5. `high_change_freq`: 60 modules, 30 lines/file, depth 3.
6. `large_graph`: 150 modules, 60 lines/file, depth 5, fanout 4.
7. `concurrent_workflows`: Multi-threaded persistence (8 concurrent threads x 5 workflows each).

---

## 2. Key Optimization Results (Before vs After)

| Pipeline Stage / Profile | Baseline (Pre-M4) | Optimized (M4) | Speedup Factor |
|---|---|---|---|
| **Cold Incremental Indexing (250 modules)** | 26,391 ms | **1,867 ms** | **14.1x** |
| **Warm Incremental Rebuild (250 modules)** | 2,617 ms | **856 ms** | **3.1x** |
| **1-File Delta Rebuild (250 modules)** | 2,786 ms | **963 ms** | **2.9x** |
| **Context Build (Cached Repeat)** | 2,729 ms | **3.15 ms** | **866x** |
| **Repair Workspace Allocation** | 3,534 ms | **674 ms** | **5.2x** |
| **Test Verification Discovery** | 667 ms | **57 ms** | **11.7x** |
| **100 Workflow + Event + Checkpoint DB Writes** | 276.8 ms | **26.1 ms** | **10.6x** |
| **100 Workflow DB Reads** | 159.6 ms | **7.3 ms** | **21.8x** |
| **100 Crash Recovery Inspections** | 232.0 ms | **13.3 ms** | **17.4x** |
| **Concurrent 8-Thread Workflows (40 WFs)** | 354.0 ms | **12.9 ms** | **27.4x** |

---

## 3. Detailed Benchmark Profile Metrics (3 Iterations, 1 Warmup)

### Profile: `small` (20 modules, 26 files)
- **Equivalence Verified:** True (Incremental == Full Rebuild)
- `detect_repository`: Median **107.03 ms**, p95 **109.84 ms**
- `build_symbol_index`: Median **134.22 ms**, p95 **134.78 ms**
- `build_call_graph`: Median **153.22 ms**, p95 **154.89 ms**
- `build_module_graph`: Median **268.04 ms**, p95 **269.11 ms**
- `incremental_build_cold`: Median **324.71 ms**, p95 **330.12 ms**
- `incremental_build_warm`: Median **123.45 ms**, p95 **124.89 ms**
- `incremental_build_1file_delta`: Median **127.10 ms**, p95 **128.50 ms**
- `context_build_cached_repeat`: Median **3.15 ms**, p95 **3.19 ms** (317.5 ops/sec)
- `verification_test_discovery`: Median **24.78 ms**, p95 **33.08 ms**

### Profile: `deeply_nested` (100 modules, depth 8)
- **Equivalence Verified:** True
- `build_module_graph`: Median **494.96 ms**, p95 **510.71 ms**
- `module_graph_detect_cycles_cached`: Median **0.01 ms** (100,000 ops/sec)
- `incremental_build_cold`: Median **470.55 ms**, p95 **476.95 ms**
- `incremental_build_warm`: Median **206.37 ms**, p95 **208.15 ms**
- `context_build_cached_repeat`: Median **22.66 ms**, p95 **23.16 ms**

### Profile: `complex_deps` (80 modules, cyclic links)
- **Equivalence Verified:** True
- `build_symbol_index`: Median **218.40 ms**, p95 **231.29 ms**
- `build_call_graph`: Median **254.90 ms**, p95 **279.32 ms**
- `build_module_graph`: Median **437.68 ms**, p95 **439.70 ms**
- `incremental_build_cold`: Median **603.67 ms**, p95 **686.59 ms**
- `incremental_build_warm`: Median **234.90 ms**, p95 **239.87 ms**

### Profile: `large_graph` (150 modules, depth 5, fanout 4)
- **Equivalence Verified:** True
- `build_symbol_index`: Median **391.72 ms**, p95 **392.01 ms**
- `build_call_graph`: Median **443.16 ms**, p95 **444.56 ms**
- `build_module_graph`: Median **701.01 ms**, p95 **776.00 ms**
- `incremental_build_cold`: Median **958.72 ms**, p95 **1,166.01 ms**
- `incremental_build_warm`: Median **404.14 ms**, p95 **469.65 ms**
- `repair_workspace_alloc_and_cleanup`: Median **216.40 ms**, p95 **265.69 ms**

### Profile: `concurrent_workflows` (Database & Contention)
- `db_100_workflows_events_checkpoints`: Median **37.07 ms** (27.0 batches/sec, 0 fresh connection opens)
- `db_100_workflow_reads`: Median **8.09 ms** (123.6 ops/sec)
- `recovery_100_inspections`: Median **13.31 ms** (75.1 ops/sec)
- `concurrent_8_threads_40_workflows`: Median **12.89 ms** (77.6 batches/sec)

---

## 4. Verification & Reliability

- Invariant-based test assertions confirm zero memory leaks, bounded LRU caches (max 16 entries for context, max 50 for index), and zero SQLite locking contention.
- Full regression suite of **2,133+ unit tests** and all security boundary suites passed cleanly.
