#!/usr/bin/env Rscript
# Inspect a Seurat object without modifying it.
#
# Emits a JSON summary: versions, dimensions, assays/layers, reductions and every
# metadata column with its levels. Nothing about the object is guessed: the
# caller decides which column is the annotation, the sample and the condition.
#
# Usage: Rscript inspect_object.R --object FILE [--object-name NAME] --out FILE

suppressPackageStartupMessages({
  library(jsonlite)
  library(Seurat)          # Assays / Reductions / DefaultAssay / Idents
  library(SeuratObject)
})

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- match(flag, args)
  if (is.na(i) || i == length(args)) return(default)
  args[[i + 1]]
}
obj_path <- get_arg("--object")
obj_name <- get_arg("--object-name")
out_path <- get_arg("--out")
if (is.null(obj_path) || is.null(out_path)) {
  stop("usage: inspect_object.R --object FILE [--object-name NAME] --out FILE")
}

fail <- function(msg) {
  cat(toJSON(list(ok = FALSE, error = msg), auto_unbox = TRUE, pretty = TRUE))
  quit(status = 0)          # the harness reads ok=false; do not signal a crash
}

if (!file.exists(obj_path)) fail(paste0("object not found: ", obj_path))

ext <- tolower(tools::file_ext(obj_path))
loaded <- new.env(parent = emptyenv())
if (ext %in% c("rds")) {
  obj <- readRDS(obj_path)
} else if (ext %in% c("rdata", "rda")) {
  nms <- load(obj_path, envir = loaded)
  if (!is.null(obj_name)) {
    if (!obj_name %in% nms) fail(paste0("object '", obj_name, "' not in file; present: ",
                                       paste(nms, collapse = ", ")))
    obj <- get(obj_name, envir = loaded)
  } else {
    is_seurat <- vapply(nms, function(n) inherits(get(n, envir = loaded), "Seurat"),
                        logical(1))
    if (sum(is_seurat) == 1) {
      obj <- get(nms[is_seurat], envir = loaded)
      obj_name <- nms[is_seurat]
    } else if (sum(is_seurat) == 0) {
      fail(paste0("no Seurat object in file; present: ", paste(nms, collapse = ", ")))
    } else {
      fail(paste0("multiple Seurat objects; pass --object-name. candidates: ",
                  paste(nms[is_seurat], collapse = ", ")))
    }
  }
} else if (ext %in% c("qs", "qs2")) {
  suppressPackageStartupMessages(library(qs2))
  obj <- qs_read(obj_path)
  if (!inherits(obj, "Seurat")) fail("file does not contain a Seurat object")
} else {
  fail(paste0("unsupported object format: .", ext))
}

if (!inherits(obj, "Seurat")) {
  fail(paste0("loaded object is not a Seurat object (class: ",
              paste(class(obj), collapse = "/"), ")"))
}

# ---- assays and layers ------------------------------------------------------
assay_info <- lapply(Assays(obj), function(a) {
  layers <- tryCatch({
    if ("Layers" %in% getNamespaceExports("SeuratObject")) {
      SeuratObject::Layers(obj[[a]])
    } else {
      slot(obj[[a]], "layers")
    }
  }, error = function(e) tryCatch(Slot(obj[[a]], "layers"), error = function(e2) character(0)))
  list(name = a,
       layers = as.list(layers),
       n_features = nrow(obj[[a]]),
       n_cells = ncol(obj[[a]]),
       default = identical(a, DefaultAssay(obj)))
})

# ---- reductions -------------------------------------------------------------
red_names <- tryCatch(Reductions(obj), error = function(e) character(0))

# ---- metadata columns -------------------------------------------------------
md <- obj[[]]
columns <- list()
for (cn in colnames(md)) {
  v <- md[[cn]]
  lv <- levels(v)
  if (is.null(lv)) {
    u <- unique(v[!is.na(v)])
    if (is.character(v) || is.logical(v)) {
      lv <- sort(u)
    } else {
      lv <- NULL
    }
  } else {
    u <- lv
  }
  # cap level listing so a per-cell numeric column cannot blow up the summary
  truncated <- FALSE
  if (length(lv) > 300) {
    lv <- lv[seq_len(300)]
    truncated <- TRUE
  }
  # per-level cell counts: the candidate enumeration is exactly this table
  counts <- NULL
  if ((is.character(v) || is.factor(v)) && length(u) <= 300) {
    tb <- table(v, useNA = "no")
    counts <- as.list(setNames(as.integer(tb), names(tb)))
  }
  is_discrete <- is.character(v) || is.factor(v) || is.logical(v)
  # n_levels is only meaningful for discrete columns; a numeric column's
  # unique-value count is reported separately so it is never mistaken for a group
  n_levels_out <- if (is_discrete) length(u) else NA_integer_
  columns[[length(columns) + 1]] <- list(
    column_name = cn,
    dtype = paste(class(v), collapse = "/"),
    is_discrete = is_discrete,
    n_distinct = length(unique(v[!is.na(v)])),
    n_levels = n_levels_out,
    n_na = sum(is.na(v)),
    levels = as.list(lv),
    levels_truncated = truncated,
    level_counts = counts,
    is_character_or_factor = is.character(v) || is.factor(v)
  )
}

out <- list(
  ok = TRUE,
  object_name = if (is.null(obj_name)) NA_character_ else obj_name,
  seurat_version = as.character(packageVersion("Seurat")),
  seuratobject_version = as.character(packageVersion("SeuratObject")),
  r_version = R.version.string,
  n_cells = ncol(obj),
  n_genes = nrow(obj),
  default_assay = DefaultAssay(obj),
  assays = assay_info,
  reductions = as.list(red_names),
  idents = as.list(levels(Idents(obj))),
  columns = columns
)

dir.create(dirname(out_path), recursive = TRUE, showWarnings = FALSE)
writeLines(toJSON(out, auto_unbox = TRUE, pretty = TRUE, null = "null"), out_path)
cat(toJSON(list(ok = TRUE, out = out_path, n_cells = ncol(obj), n_genes = nrow(obj)),
           auto_unbox = TRUE))
