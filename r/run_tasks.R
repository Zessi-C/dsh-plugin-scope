#!/usr/bin/env Rscript
# Run a batch of SCTDA tasks against one Seurat object.
#
# The object is loaded exactly once for the whole batch: loading a 500 MB to
# 6 GB object per task would dominate runtime and risk memory exhaustion.
# Every task's outcome is written to <outdir>/<task_id>/result.json; the
# combined list goes to stdout as a single JSON document.
#
# Usage: Rscript run_tasks.R --object FILE --config cfg.json --tasks tasks.json --out-dir DIR

suppressPackageStartupMessages({
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- match(flag, args)
  if (is.na(i) || i == length(args)) return(default)
  args[[i + 1]]
}
obj_path  <- get_arg("--object")
obj_name  <- get_arg("--object-name")
cfg_path  <- get_arg("--config")
tasks_path<- get_arg("--tasks")
out_dir   <- get_arg("--out-dir")
if (any(vapply(list(obj_path, cfg_path, tasks_path, out_dir), is.null, logical(1)))) {
  stop("usage: run_tasks.R --object FILE --config cfg.json --tasks tasks.json --out-dir DIR")
}

script_dir <- {
  a <- commandArgs(trailingOnly = FALSE)
  f <- sub("^--file=", "", a[grep("^--file=", a)])
  if (length(f) == 0) "." else dirname(normalizePath(f[1]))
}
source(file.path(script_dir, "adapters.R"))

cfg <- fromJSON(cfg_path, simplifyVector = TRUE)
tasks <- fromJSON(tasks_path, simplifyVector = FALSE)
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

emit <- function(x) cat(toJSON(x, auto_unbox = TRUE, null = "null", na = "null",
                               digits = NA))

# ---- load object (read-only) ------------------------------------------------
obj <- tryCatch({
  ext <- tolower(tools::file_ext(obj_path))
  if (ext == "rds") {
    readRDS(obj_path)
  } else if (ext %in% c("rdata", "rda")) {
    e <- new.env(parent = emptyenv())
    nms <- load(obj_path, envir = e)
    if (!is.null(obj_name) && nzchar(obj_name)) {
      get(obj_name, envir = e)
    } else {
      is_seu <- vapply(nms, function(n) inherits(get(n, envir = e), "Seurat"), logical(1))
      if (sum(is_seu) != 1) stop("cannot pick a single Seurat object from file; pass --object-name")
      get(nms[is_seu], envir = e)
    }
  } else if (ext %in% c("qs", "qs2")) {
    suppressPackageStartupMessages(library(qs2)); qs_read(obj_path)
  } else stop("unsupported object format: ", ext)
}, error = function(e) e)
if (inherits(obj, "error")) {
  emit(list(ok = FALSE, stage = "load", error = conditionMessage(obj)))
  quit(status = 0)
}

if (is.null(cfg$assay) || is.na(cfg$assay) || !nzchar(cfg$assay)) {
  cfg$assay <- DefaultAssay(obj)
}

results <- vector("list", length(tasks))
for (i in seq_along(tasks)) {
  t <- tasks[[i]]
  tid <- t$task_id
  md <- t$method_id
  odir <- file.path(out_dir, tid)
  dir.create(odir, recursive = TRUE, showWarnings = FALSE)

  started <- format(Sys.time(), "%Y-%m-%dT%H:%M:%S%z")
  adapter <- SCTDA_ADAPTERS[[md]]
  res <- if (is.null(adapter)) {
    list(status = "failed", reason = paste0("no adapter registered for method_id ", md))
  } else {
    tryCatch(
      adapter(obj, cfg, t$params, odir),
      error = function(e) list(status = "failed",
                               reason = paste0(class(e)[1], ": ", conditionMessage(e))))
  }
  finished <- format(Sys.time(), "%Y-%m-%dT%H:%M:%S%z")

  # artifact hashes: the index the Python side stores must be checkable
  arts <- list()
  files <- list.files(odir, full.names = TRUE)
  for (f in files) {
    if (basename(f) == "result.json" || isTRUE(file.info(f)$isdir)) next
    n_rows <- tryCatch(length(readLines(f, warn = FALSE)) - 1L,
                       error = function(e) NA_integer_)
    arts[[length(arts) + 1]] <- list(path = f, checksum = sctda_md5(f),
                                     checksum_algo = "md5", n_rows = n_rows)
  }
  rec <- list(task_id = tid, method_id = md,
              candidate_label = t$params$candidate_label,
              status = res$status %||% "failed",
              reason = res$reason %||% NA,
              artifacts = arts,
              metrics = res$metrics %||% list(),
              started_utc = started, finished_utc = finished)
  writeLines(toJSON(rec, auto_unbox = TRUE, null = "null", na = "null", digits = NA),
             file.path(odir, "result.json"))
  results[[i]] <- rec
}

emit(list(ok = TRUE, n_tasks = length(tasks), results = results))
