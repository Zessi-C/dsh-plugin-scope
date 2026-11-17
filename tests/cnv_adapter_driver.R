#!/usr/bin/env Rscript
# Test driver for the inferCNV adapter (r/adapters.R::task_cnv_infercnv).
#
# The real CNV engine is replaced by an in-process shim so the driver observes
# what the adapter itself does — which package it demands, which arguments it
# passes to infercnv::run, what it writes — without paying for a real run.
# The shim is installed the only way R allows from outside a package: by
# shadowing `requireNamespace` and `::` in the global environment, which is the
# enclosing environment of the functions source()d from adapters.R.
#
# Usage: Rscript tests/cnv_adapter_driver.R gate|window
# Exit status 77 means "this R installation lacks the packages the driver
# needs"; the Python tests turn that into a skip.

args <- commandArgs(trailingOnly = TRUE)
mode <- if (length(args) >= 1) args[[1]] else "window"

needed <- c("Seurat", "SeuratObject", "Matrix", "jsonlite")
if (!all(vapply(needed, requireNamespace, logical(1), quietly = TRUE))) {
  cat("missing R packages:", paste(needed[!vapply(needed, requireNamespace, logical(1), quietly = TRUE)], collapse = ", "), "\n")
  quit(status = 77)
}

emit <- function(x) cat(jsonlite::toJSON(x, auto_unbox = TRUE, null = "null", na = "null", digits = NA))

# Run from the repository root so the adapter's relative gene-order path resolves.
script_dir <- {
  a <- commandArgs(trailingOnly = FALSE)
  f <- sub("^--file=", "", a[grep("^--file=", a)])
  if (length(f) == 0) "." else dirname(normalizePath(f[1]))
}
repo_root <- normalizePath(file.path(script_dir, ".."))
setwd(repo_root)

source(file.path(repo_root, "r", "adapters.R"))

base_requireNamespace <- get("requireNamespace", envir = baseenv())
base_getExportedValue <- get("getExportedValue", envir = baseenv())

# ---------------------------------------------------------------- mode: gate
# infercnv absent, CopyKAT present. CopyKAT must not open the gate, and the
# refusal must name inferCNV rather than falling through to a later guard.
if (identical(mode, "gate")) {
  `requireNamespace` <- function(package, quietly = FALSE) {
    if (identical(package, "infercnv")) return(FALSE)
    if (package %in% c("copykat", "CopyKAT")) return(TRUE)
    base_requireNamespace(package, quietly = quietly)
  }
  res <- tryCatch(
    # obj is NULL on purpose: a gate that lets this through dies here, which is
    # exactly the bug this mode pins down.
    task_cnv_infercnv(NULL, list(annotation_column = "celltype", assay = "RNA"),
                      list(), file.path(tempdir(), "gate_out")),
    error = function(e) list(status = "error", reason = paste(class(e)[1], conditionMessage(e)))
  )
  emit(list(mode = mode, status = res$status, reason = res$reason))
  quit(status = 0)
}

# ------------------------------------------------------------ mode: window
# A small real Seurat object whose genes come from the shipped hg38 gene order,
# so the adapter's gene-order overlap test passes on genuine symbols.
run_calls <- list()
last_create <- NULL

`::` <- function(pkg, name) {
  p <- as.character(substitute(pkg))
  n <- as.character(substitute(name))
  if (identical(p, "infercnv")) {
    if (identical(n, "CreateInfercnvObject")) {
      return(function(raw_counts_matrix, gene_order_file, annotations_file, ref_group_names, ...) {
        anno <- utils::read.delim(annotations_file, header = FALSE, stringsAsFactors = FALSE)
        last_create <<- list(
          genes = nrow(raw_counts_matrix), cells = ncol(raw_counts_matrix),
          gene_order_cols = ncol(utils::read.delim(gene_order_file, header = FALSE)),
          n_anno = nrow(anno),
          ref_group_names = as.character(unlist(ref_group_names))
        )
        list(expr = raw_counts_matrix,
             refs = as.character(unlist(ref_group_names)),
             anno = stats::setNames(anno[[2]], anno[[1]]))
      })
    }
    if (identical(n, "run")) {
      return(function(obj, ...) {
        run_calls[[length(run_calls) + 1L]] <<- list(...)
        m <- obj$expr
        v <- matrix(1, nrow = nrow(m), ncol = ncol(m), dimnames = dimnames(m))
        query <- !(obj$anno[colnames(m)] %in% obj$refs)
        v[, query] <- 1.3   # a query-group shift the adapter should report
        methods::new("SctdaFakeInfercnv", expr.data = v)
      })
    }
  }
  if (identical(p, "utils") && identical(n, "packageVersion")) {
    return(function(...) "9.9.9-shim")
  }
  base_getExportedValue(p, n)
}
`requireNamespace` <- function(package, quietly = FALSE) {
  if (identical(package, "infercnv")) return(TRUE)
  base_requireNamespace(package, quietly = quietly)
}
methods::setClass("SctdaFakeInfercnv", representation(expr.data = "matrix"))

go <- utils::read.table(file.path(repo_root, "r", "hg38_gene_order.tsv"), sep = "\t",
                        stringsAsFactors = FALSE, col.names = c("gene", "chr", "start", "end"))
chr1 <- go[go$chr == "chr1", , drop = FALSE]
chr1 <- chr1[!duplicated(chr1$gene), , drop = FALSE]
genes <- chr1$gene[round(seq(1, nrow(chr1), length.out = 160))]
cells <- c(paste0("T", seq_len(12)), paste0("E", seq_len(18)))
set.seed(42)
counts <- matrix(stats::rnbinom(length(genes) * length(cells), size = 5, mu = 4),
                 nrow = length(genes), dimnames = list(genes, cells))
obj <- SeuratObject::CreateSeuratObject(
  counts = Matrix::Matrix(counts, sparse = TRUE),
  meta.data = data.frame(celltype = c(rep("Tcell", 12), rep("Epithelial", 18)),
                         row.names = cells)
)
cfg <- list(annotation_column = "celltype", assay = "RNA")

call_adapter <- function(params, outdir) {
  run_calls <<- list()
  res <- tryCatch(task_cnv_infercnv(obj, cfg, params, outdir),
                  error = function(e) list(status = "error",
                                           reason = paste(class(e)[1], conditionMessage(e))))
  out <- list(status = res$status, reason = res$reason,
              metrics = res$metrics, artifacts = as.character(unlist(res$artifacts)),
              n_run_calls = length(run_calls),
              run_args = if (length(run_calls)) run_calls[[1]] else NULL,
              create = last_create)
  if (identical(res$status, "succeeded")) {
    per_cell <- utils::read.csv(file.path(outdir, "cnv_scores_per_cell.csv"),
                                stringsAsFactors = FALSE)
    by_group <- utils::read.csv(file.path(outdir, "cnv_summary_by_group.csv"),
                                stringsAsFactors = FALSE)
    out$n_cells_csv <- nrow(per_cell)
    out$groups <- by_group$group
    out$query_scores <- by_group$mean_cnv_score[!by_group$is_reference]
    out$reference_scores <- by_group$mean_cnv_score[by_group$is_reference]
  }
  out
}

default_call <- call_adapter(list(), file.path(tempdir(), "cnv_default_out"))
custom_call <- call_adapter(list(window_size = 120L), file.path(tempdir(), "cnv_custom_out"))

emit(list(mode = mode, default = default_call, custom = custom_call))
