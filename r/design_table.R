#!/usr/bin/env Rscript
# Design table: the metadata cross-tabs a plan needs, without touching expression.
#
# Emits:
#   samples[]              sample -> condition, n_cells
#   candidate_by_sample[]  candidate x sample cell counts (the pseudobulk rule input)
#   condition_levels[]     group -> n_samples, n_cells
#   warnings[]             design problems worth surfacing before a run
#
# Usage: Rscript design_table.R --object FILE --config cfg.json --out FILE

suppressPackageStartupMessages({library(jsonlite)})

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- match(flag, args); if (is.na(i) || i == length(args)) return(default); args[[i + 1]]
}
obj_path <- get_arg("--object"); obj_name <- get_arg("--object-name")
cfg_path <- get_arg("--config"); out_path <- get_arg("--out")
script_dir <- { a <- commandArgs(trailingOnly = FALSE)
  f <- sub("^--file=", "", a[grep("^--file=", a)]); if (!length(f)) "." else dirname(normalizePath(f[1])) }
suppressPackageStartupMessages({library(Seurat); library(SeuratObject)})
source(file.path(script_dir, "adapters.R"))

fail <- function(msg) { cat(toJSON(list(ok = FALSE, error = msg), auto_unbox = TRUE)); quit(status = 0) }
cfg <- fromJSON(cfg_path, simplifyVector = TRUE)

obj <- tryCatch({
  ext <- tolower(tools::file_ext(obj_path))
  if (ext == "rds") readRDS(obj_path) else {
    e <- new.env(parent = emptyenv()); nms <- load(obj_path, envir = e)
    if (!is.null(obj_name) && nzchar(obj_name)) get(obj_name, envir = e) else {
      s <- vapply(nms, function(n) inherits(get(n, envir = e), "Seurat"), logical(1))
      if (sum(s) != 1) stop("cannot pick a single Seurat object; pass --object-name")
      get(nms[s], envir = e) }
  }
}, error = function(e) e)
if (inherits(obj, "error")) fail(conditionMessage(obj))

md <- obj[[]]
anno <- if (!is.null(cfg$annotation_column) && nzchar(cfg$annotation_column)) md[[cfg$annotation_column]] else NULL
sample <- if (!is.null(cfg$sample_column) && nzchar(cfg$sample_column)) md[[cfg$sample_column]] else NULL
cond <- if (!is.null(cfg$condition_column) && nzchar(cfg$condition_column)) md[[cfg$condition_column]] else NULL

warnings <- character(0)
samples <- list()
cand_by_sample <- list()
cond_levels <- list()

if (!is.null(sample)) {
  ss <- as.character(sample)
  cc <- if (!is.null(cond)) as.character(cond) else rep(NA_character_, length(ss))
  tab <- unique(data.frame(sample = ss, condition = cc, stringsAsFactors = FALSE))
  cnt <- as.data.frame(table(sample = ss), stringsAsFactors = FALSE)
  names(cnt)[2] <- "n_cells"
  tab <- merge(tab, cnt, by = "sample")
  tab <- tab[order(tab$sample), ]
  samples <- split(tab[, c("sample", "condition", "n_cells")], seq_len(nrow(tab)))
  samples <- lapply(samples, function(r) as.list(r))

  if (!all(is.na(cc))) {
    cl <- as.data.frame(table(condition = cc), stringsAsFactors = FALSE)
    names(cl)[2] <- "n_cells"
    ns <- as.data.frame(table(condition = unique(data.frame(s = ss, c = cc))$c),
                        stringsAsFactors = FALSE)
    names(ns) <- c("condition", "n_samples")
    cl <- merge(cl, ns, by = "condition")
    cond_levels <- lapply(split(cl, seq_len(nrow(cl))), function(r) as.list(r))
    for (lv in cl$condition) if (cl$n_samples[cl$condition == lv] < 3)
      warnings <- c(warnings, sprintf("condition %s has %d samples (<3): no group-level inference",
                                      lv, cl$n_samples[cl$condition == lv]))
    # sample must map to exactly one condition
    bad <- unique(tab$sample[duplicated(tab[, c("sample")])])
    if (length(bad)) warnings <- c(warnings,
      sprintf("%d samples appear with multiple conditions", length(bad)))
  } else {
    warnings <- c(warnings, "no condition column: no group-level statement can be made")
  }

  if (!is.null(anno)) {
    a <- as.character(anno)
    keep <- !is.na(a) & !is.na(ss)
    d <- data.frame(candidate = a[keep], sample = ss[keep], stringsAsFactors = FALSE)
    tb <- as.data.frame(table(d), stringsAsFactors = FALSE)
    names(tb) <- c("candidate", "sample", "n_cells")
    cand_by_sample <- lapply(split(tb, seq_len(nrow(tb))), function(r) as.list(r))
    low <- tb[tb$n_cells < 10, ]
    if (nrow(low)) warnings <- c(warnings,
      sprintf("%d candidate x sample pairs have <10 cells and will be excluded from pseudobulk",
              nrow(low)))
  }
} else {
  warnings <- c(warnings, "no sample column: only pooled descriptive results are possible")
}

out <- list(ok = TRUE, n_cells = ncol(obj),
            annotation_column = cfg$annotation_column,
            sample_column = cfg$sample_column,
            condition_column = cfg$condition_column,
            samples = samples, condition_levels = cond_levels,
            candidate_by_sample = cand_by_sample, warnings = as.list(warnings))
dir.create(dirname(out_path), recursive = TRUE, showWarnings = FALSE)
writeLines(toJSON(out, auto_unbox = TRUE, null = "null", na = "null"), out_path)
cat(toJSON(list(ok = TRUE, out = out_path, n_samples = length(samples),
                n_warnings = length(warnings)), auto_unbox = TRUE))
