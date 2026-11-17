# SCTDA method adapters (R side).
#
# Every adapter returns a list(status=, reason=, artifacts=list(), metrics=list()).
# Status is one of:
#   succeeded            - the computation ran and produced its artifacts
#   not_applicable       - the method does not apply to this dataset/config
#   insufficient_input   - a required input is missing (never faked, never zero-filled)
#   failed               - the computation raised; the original message is kept
#
# A computed zero is data (status succeeded with a zero); a missing input is not.

suppressPackageStartupMessages({
  library(Seurat)
  library(SeuratObject)
  library(Matrix)
  library(jsonlite)
})

`%||%` <- function(a, b) if (is.null(a) || (length(a) == 1 && is.na(a))) b else a

# ---------------------------------------------------------------- shared helpers

# Expression matrix for scoring/DE. Prefers existing normalized data; if the
# object only carries counts, it normalizes in memory and records that the layer
# was derived, so the report can say where the numbers came from.
sctda_expr <- function(obj, assay) {
  layers <- tryCatch(SeuratObject::Layers(obj[[assay]]), error = function(e) character(0))
  if ("data" %in% layers) {
    return(list(mat = SeuratObject::LayerData(obj, assay = assay, layer = "data"),
                layer = "data", derived = FALSE))
  }
  if ("counts" %in% layers) {
    m <- SeuratObject::LayerData(obj, assay = assay, layer = "counts")
    m <- SeuratObject::LogNormalize(m, scale.factor = 1e4, verbose = FALSE)
    return(list(mat = m, layer = "counts->lognorm(1e4)", derived = TRUE))
  }
  stop("assay ", assay, " has neither 'data' nor 'counts' layer")
}

sctda_has_counts <- function(obj, assay) {
  "counts" %in% tryCatch(SeuratObject::Layers(obj[[assay]]),
                         error = function(e) character(0))
}

sctda_meta <- function(obj, column) {
  md <- obj[[]]
  if (is.null(column) || is.na(column) || !column %in% colnames(md)) {
    stop("metadata column not found: ", column)
  }
  v <- md[[column]]
  as.character(v)
}

sctda_write_csv <- function(df, path) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  utils::write.csv(df, path, row.names = FALSE)
  path
}

sctda_md5 <- function(path) {
  as.character(tools::md5sum(path))
}

# Guard rails the design fixes as non-negotiable:
#  - the sample is the statistical unit
#  - fewer than 3 samples per group cannot support a group comparison
#  - fewer than MIN_CELLS_PER_SAMPLE cells of a candidate in a sample excludes
#    that sample from pseudobulk aggregation
MIN_CELLS_PER_SAMPLE <- 10L
MIN_SAMPLES_PER_GROUP <- 3L

# ---------------------------------------------------------------- composition

task_composition <- function(obj, cfg, params, outdir) {
  anno <- sctda_meta(obj, cfg$annotation_column)
  sample_col <- cfg$sample_column
  cond_col <- cfg$condition_column
  if (is.null(sample_col) || is.na(sample_col)) {
    # overall counts are still a real (if coarse) result; per-sample is not
    tb <- as.data.frame(table(candidate = anno), stringsAsFactors = FALSE)
    names(tb)[2] <- "n_cells"
    tb$proportion_of_all <- tb$n_cells / ncol(obj)
    p <- sctda_write_csv(tb, file.path(outdir, "composition_overall.csv"))
    return(list(status = "succeeded",
                reason = paste("no sample column mapped: overall composition only,",
                               "no per-sample or group-level statement"),
                artifacts = list(p),
                metrics = list(n_candidates = nrow(tb), per_sample = FALSE)))
  }
  sample <- sctda_meta(obj, sample_col)
  cond <- if (!is.null(cond_col) && !is.na(cond_col)) sctda_meta(obj, cond_col) else NA_character_

  df <- data.frame(candidate = anno, sample = sample,
                   condition = cond, stringsAsFactors = FALSE)
  df <- df[!is.na(df$candidate) & !is.na(df$sample), , drop = FALSE]
  agg <- stats::aggregate(list(n_cells_candidate = df$candidate), by = df[c("candidate", "sample")], FUN = length)
  tot <- stats::aggregate(list(n_cells_sample = df$candidate), by = df["sample"], FUN = length)
  agg <- merge(agg, tot, by = "sample")
  agg$proportion <- agg$n_cells_candidate / agg$n_cells_sample
  if (!all(is.na(cond))) {
    smap <- unique(df[!is.na(df$sample), c("sample", "condition")])
    agg <- merge(agg, smap, by = "sample", all.x = TRUE)
  }
  agg <- agg[order(agg$candidate, agg$sample), , drop = FALSE]
  p1 <- sctda_write_csv(agg, file.path(outdir, "composition_by_sample.csv"))

  overall <- as.data.frame(table(candidate = anno), stringsAsFactors = FALSE)
  names(overall)[2] <- "n_cells"
  overall$proportion_of_all <- overall$n_cells / sum(overall$n_cells)
  p2 <- sctda_write_csv(overall, file.path(outdir, "composition_overall.csv"))

  n_samples <- length(unique(agg$sample))
  list(status = "succeeded",
       reason = sprintf("per-sample composition for %d candidates over %d samples",
                        length(unique(agg$candidate)), n_samples),
       artifacts = c(p1, p2),
       metrics = list(n_candidates = length(unique(agg$candidate)),
                      n_samples = n_samples)
  )
}

# ---------------------------------------------------------------- differential expression

# Cell-level Wilcoxon (the literature-standard test) PLUS a sample-level
# pseudobulk effect size. The cell-level p-value is reported because it is what
# the literature reports, but it is flagged: cells are not independent
# replicates. The sample-level table is what supports an inference.
task_de_vs_rest_wilcox <- function(obj, cfg, params, outdir) {
  anno <- sctda_meta(obj, cfg$annotation_column)
  candidate <- params$candidate_label
  assay <- cfg$assay
  if (is.null(candidate)) return(list(status = "failed", reason = "candidate_label missing"))

  in_cand <- !is.na(anno) & anno == candidate
  n_cand <- sum(in_cand)
  if (n_cand < params$min_cells) {
    return(list(status = "insufficient_input",
                reason = sprintf("candidate %s has %d cells (< min_cells %d)",
                                 candidate, n_cand, params$min_cells)))
  }
  Idents(obj) <- factor(ifelse(in_cand, "candidate", "rest"),
                        levels = c("rest", "candidate"))
  res <- tryCatch(
    Seurat::FindMarkers(obj, ident.1 = "candidate", ident.2 = "rest",
                        assay = assay, test.use = "wilcox",
                        logfc.threshold = params$logfc_threshold,
                        min.pct = params$min_pct,
                        verbose = FALSE),
    error = function(e) e)
  if (inherits(res, "error")) {
    return(list(status = "failed", reason = paste("FindMarkers:", conditionMessage(res))))
  }
  df <- data.frame(gene = rownames(res), res, row.names = NULL, stringsAsFactors = FALSE)
  df <- df[order(df$p_val_adj, -abs(df$avg_log2FC)), , drop = FALSE]
  df$candidate <- candidate
  df$n_cells_candidate <- n_cand
  df$test <- "wilcox_cell_level"
  df$independence_warning <- "cells treated as independent replicates; p-values are anti-conservative"
  p1 <- sctda_write_csv(utils::head(df, params$top_n), file.path(outdir, "de_cell_level.csv"))

  # ---- sample-level pseudobulk effect size -------------------------------
  # The sample is the statistical unit. Pseudobulk sums raw counts per sample,
  # converts to CPM and only then takes log2 ratios: taking a ratio of
  # log-normalized means with a 1e-9 pseudocount produces astronomically large
  # "fold changes" on zero-inflated data and is meaningless.
  sample_col <- cfg$sample_column
  sample_support <- NA_integer_
  p2 <- NULL
  p3 <- NULL
  basis <- NA_character_
  if (!is.null(sample_col) && !is.na(sample_col)) {
    sample <- sctda_meta(obj, sample_col)
    use_counts <- sctda_has_counts(obj, assay)
    mat <- if (use_counts) {
      basis <- "counts_summed_per_sample_cpm"
      SeuratObject::LayerData(obj, assay = assay, layer = "counts")
    } else {
      basis <- "log_normalized_mean_difference"
      sctda_expr(obj, assay)$mat
    }
    keep <- !is.na(sample) & !is.na(anno)
    genes <- unique(df$gene[seq_len(min(nrow(df), params$top_n))])
    genes <- intersect(genes, rownames(mat))
    pb <- list()
    for (s in unique(sample[keep])) {
      idx_c <- which(keep & sample == s & anno == candidate)
      idx_r <- which(keep & sample == s & anno != candidate)
      if (length(idx_c) < MIN_CELLS_PER_SAMPLE || length(idx_r) < MIN_CELLS_PER_SAMPLE) next
      vc <- Matrix::rowSums(mat[genes, idx_c, drop = FALSE])
      vr <- Matrix::rowSums(mat[genes, idx_r, drop = FALSE])
      if (use_counts) {
        mc <- as.numeric(vc) / sum(as.numeric(vc)) * 1e6
        mr <- as.numeric(vr) / sum(as.numeric(vr)) * 1e6
        lfc <- log2((mc + 1) / (mr + 1))
      } else {
        mc <- as.numeric(Matrix::rowMeans(mat[genes, idx_c, drop = FALSE]))
        mr <- as.numeric(Matrix::rowMeans(mat[genes, idx_r, drop = FALSE]))
        lfc <- mc - mr          # difference of log-normalized means (log2 scale)
      }
      pb[[s]] <- data.frame(sample = s, gene = genes,
                            mean_candidate = mc, mean_rest = mr, log2FC = lfc,
                            n_cells_candidate = length(idx_c),
                            n_cells_rest = length(idx_r),
                            stringsAsFactors = FALSE)
    }
    if (length(pb) > 0) {
      pbdf <- do.call(rbind, pb)
      rownames(pbdf) <- NULL
      pbdf$basis <- basis
      p2 <- sctda_write_csv(pbdf, file.path(outdir, "de_per_sample.csv"))
      sample_support <- length(unique(pbdf$sample))
      agg <- do.call(rbind, lapply(split(pbdf, pbdf$gene), function(g) {
        n <- nrow(g)
        m <- mean(g$log2FC)
        se <- if (n > 1) stats::sd(g$log2FC) / sqrt(n) else NA_real_
        tcrit <- if (n > 1) stats::qt(0.975, df = n - 1) else NA_real_
        data.frame(gene = g$gene[1], n_samples = n,
                   mean_log2FC_sample_level = m,
                   ci_low = m - tcrit * se, ci_high = m + tcrit * se,
                   frac_samples_positive = mean(g$log2FC > 0),
                   corroboration = if (n >= MIN_SAMPLES_PER_GROUP) "supported" else "low_sample_support",
                   caveat = if (n < MIN_SAMPLES_PER_GROUP) "fewer than 3 samples; supplementary only" else "supplementary across-sample corroboration",
                   is_supplementary = TRUE,
                   basis = g$basis[1],
                   stringsAsFactors = FALSE)
      }))
      agg$candidate <- candidate
      agg <- agg[order(-abs(agg$mean_log2FC_sample_level)), , drop = FALSE]
      p3 <- sctda_write_csv(agg, file.path(outdir, "de_sample_level.csv"))
    }
  }
  artifacts <- c(p1, p2, p3)
  list(status = "succeeded",
       reason = sprintf("%d genes tested at cell level (primary); sample-level corroboration over %s samples (basis: %s)",
                        nrow(df),
                        if (is.na(sample_support)) "no" else as.character(sample_support),
                        if (is.na(basis)) "n/a" else basis),
       artifacts = artifacts,
       metrics = list(n_genes_tested = nrow(df), n_significant = sum(df$p_val_adj < 0.05),
                      n_cells_candidate = n_cand,
                      n_samples_supporting = sample_support,
                      pseudobulk_basis = basis,
                      role = "primary_cell_level_with_supplementary_sample_corroboration",
                      caveat = "cell-level Wilcoxon is primary (field mainstream); cells treated as non-independent units (anti-conservative)",
                      is_supplementary = TRUE))
}

# ---------------------------------------------------------------- gene set scoring

# AUCell: literature-standard per-cell gene set activity (area under the
# recovery curve). Scores are relative within a cell and must not be compared
# across cells as absolute activity.
task_signature_score_aucell <- function(obj, cfg, params, outdir) {
  anno <- sctda_meta(obj, cfg$annotation_column)
  assay <- cfg$assay
  genesets <- params$gene_sets
  if (is.null(genesets) || length(genesets) == 0) {
    return(list(status = "insufficient_input", reason = "no gene sets supplied"))
  }
  ex <- sctda_expr(obj, assay)
  expr <- ex$mat
  # AUCell ranks genes within a cell; restrict to the sets' gene universe
  universe <- unique(unlist(lapply(genesets, function(g) intersect(g, rownames(expr)))))
  empty <- names(genesets)[vapply(genesets, function(g)
    length(intersect(g, rownames(expr))) == 0, logical(1))]
  genesets <- genesets[vapply(genesets, function(g)
    length(intersect(g, rownames(expr))) >= 5, logical(1))]
  if (length(genesets) == 0) {
    return(list(status = "not_applicable",
                reason = "no supplied gene set has >=5 genes measured in this object"))
  }
  rankings <- tryCatch(
    AUCell::AUCell_buildRankings(expr, plotStats = FALSE, verbose = FALSE),
    error = function(e) e)
  if (inherits(rankings, "error")) {
    return(list(status = "failed", reason = paste("AUCell_buildRankings:", conditionMessage(rankings))))
  }
  cells <- tryCatch(
    AUCell::AUCell_calcAUC(genesets, rankings, verbose = FALSE),
    error = function(e) e)
  if (inherits(cells, "error")) {
    return(list(status = "failed", reason = paste("AUCell_calcAUC:", conditionMessage(cells))))
  }
  sc <- t(AUCell::getAUC(cells))            # cells x sets
  df <- data.frame(cell = rownames(sc), sc, check.names = FALSE, stringsAsFactors = FALSE)
  df$candidate <- anno[match(df$cell, colnames(obj))]
  sample_col <- cfg$sample_column
  if (!is.null(sample_col) && !is.na(sample_col)) {
    df$sample <- sctda_meta(obj, sample_col)[match(df$cell, colnames(obj))]
  }
  p1 <- sctda_write_csv(utils::head(df, params$max_cells_written),
                        file.path(outdir, "signature_scores_per_cell.csv"))

  long <- do.call(rbind, lapply(names(genesets), function(sn) {
    data.frame(candidate = df$candidate,
               sample = if ("sample" %in% names(df)) df$sample else NA_character_,
               gene_set = sn, score = df[[sn]], stringsAsFactors = FALSE)
  }))
  long <- long[!is.na(long$candidate), , drop = FALSE]
  key <- c("candidate", "gene_set")
  if (!all(is.na(long$sample))) key <- c("candidate", "gene_set", "sample")
  summ <- do.call(rbind, lapply(split(long, interaction(long[key], drop = TRUE)), function(g) {
    data.frame(g[1, key, drop = FALSE], n_cells = nrow(g),
               mean_score = mean(g$score), median_score = stats::median(g$score),
               sd_score = if (nrow(g) > 1) stats::sd(g$score) else NA_real_,
               stringsAsFactors = FALSE)
  }))
  rownames(summ) <- NULL
  p2 <- sctda_write_csv(summ, file.path(outdir, "signature_summary.csv"))
  list(status = "succeeded",
       reason = sprintf("%d gene sets scored, %d cells, expression layer %s%s",
                        length(genesets), ncol(expr), ex$layer,
                        if (ex$derived) " (normalized in-memory; source had counts only)" else ""),
       artifacts = c(p1, p2),
       metrics = list(n_gene_sets = length(genesets), n_cells = ncol(expr),
                      dropped_gene_sets = as.list(empty), layer = ex$layer,
                      layer_derived = ex$derived))
}

# ---------------------------------------------------------------- CNV inference

task_cnv_infercnv <- function(obj, cfg, params, outdir) {
  # inferCNV is the only engine this adapter runs. CopyKAT is a different
  # algorithm, so its presence must not satisfy the gate (and its absence must
  # not close it): a missing infercnv is a missing input, never a licence to
  # run something else under the inferCNV name.
  if (!requireNamespace("infercnv", quietly = TRUE)) {
    return(list(
      status = "insufficient_input",
      reason = "inferCNV (package 'infercnv') is not installed in R; install it with BiocManager::install('infercnv'). CopyKAT is a different method and is not run as a substitute"
    ))
  }

  anno_col <- cfg$annotation_column
  if (is.null(anno_col) || is.na(anno_col) || anno_col == "") {
    return(list(status = "insufficient_input", reason = "no annotation column specified for CNV"))
  }
  anno <- sctda_meta(obj, anno_col)
  if (is.null(anno)) {
    return(list(status = "insufficient_input", reason = sprintf("column %s not found in metadata", anno_col)))
  }
  keep_cells <- which(!is.na(anno))
  if (length(keep_cells) == 0) {
    return(list(status = "insufficient_input", reason = "all cells have NA in annotation column"))
  }
  anno <- anno[keep_cells]
  cell_names <- colnames(obj)[keep_cells]
  groups <- unique(anno)

  ref_group <- params$reference_group
  if (is.null(ref_group) || length(ref_group) == 0 || is.na(ref_group) || ref_group == "") {
    ref_pattern <- "(immune|stromal|normal|fibroblast|t[ _-]?cell|b[ _-]?cell|myeloid|macrophage|monocyte|plasma|mast|dendritic|nk|non-malignant|microglia)"
    matched_refs <- grep(ref_pattern, groups, ignore.case = TRUE, value = TRUE)
    if (length(matched_refs) == 0) {
      return(list(
        status = "insufficient_input",
        reason = sprintf("no default non-malignant/immune/stromal reference group found in %s; available groups: %s. Set reference_group parameter explicitly.",
                         anno_col, paste(utils::head(groups, 10), collapse = ", "))
      ))
    }
    ref_group <- matched_refs
  } else {
    if (is.character(ref_group) && length(ref_group) == 1 && grepl(",", ref_group)) {
      ref_group <- trimws(strsplit(ref_group, ",")[[1]])
    }
    missing_refs <- setdiff(ref_group, groups)
    if (length(missing_refs) > 0) {
      return(list(
        status = "insufficient_input",
        reason = sprintf("reference_group %s not found in %s; available groups: %s",
                         paste(missing_refs, collapse = ", "), anno_col,
                         paste(utils::head(groups, 10), collapse = ", "))
      ))
    }
  }

  min_cells <- if (!is.null(params$min_cells)) as.integer(params$min_cells) else 10
  n_ref_cells <- sum(anno %in% ref_group)
  if (n_ref_cells < min_cells) {
    return(list(status = "insufficient_input",
                reason = sprintf("reference group %s has %d cells (< min_cells %d)",
                                 paste(ref_group, collapse=", "), n_ref_cells, min_cells)))
  }

  go_file <- "r/hg38_gene_order.tsv"
  go <- NULL
  if (file.exists(go_file)) {
    go <- utils::read.table(go_file, sep = "\t", stringsAsFactors = FALSE, col.names = c("gene", "chr", "start", "end"))
  } else if (requireNamespace("EnsDb.Hsapiens.v86", quietly = TRUE)) {
    g <- EnsDb.Hsapiens.v86::genes(EnsDb.Hsapiens.v86::EnsDb.Hsapiens.v86)
    chrs <- as.character(GenomeInfoDb::seqnames(g))
    valid <- chrs %in% c(1:22, "X", "Y")
    go <- data.frame(gene = g$gene_name[valid], chr = paste0("chr", chrs[valid]),
                     start = BiocGenerics::start(g)[valid], end = BiocGenerics::end(g)[valid],
                     stringsAsFactors = FALSE)
    go <- go[!duplicated(go$gene), ]
  }
  if (is.null(go) || nrow(go) == 0) {
    return(list(status = "insufficient_input",
                reason = "cannot load gene order: hg38_gene_order.tsv missing and EnsDb.Hsapiens.v86 unavailable"))
  }

  assay <- cfg$assay
  use_counts <- sctda_has_counts(obj, assay)
  mat <- if (use_counts) {
    SeuratObject::LayerData(obj, assay = assay, layer = "counts")[, keep_cells, drop = FALSE]
  } else {
    sctda_expr(obj, assay)$mat[, keep_cells, drop = FALSE]
  }

  common_genes <- intersect(rownames(mat), go$gene)
  if (length(common_genes) < 50) {
    return(list(status = "insufficient_input",
                reason = sprintf("only %d genes overlap with genomic coordinates (< 50)", length(common_genes))))
  }
  go_sub <- go[go$gene %in% common_genes, ]
  go_sub <- go_sub[!duplicated(go_sub$gene), ]
  mat_sub <- mat[go_sub$gene, , drop = FALSE]

  pkg_used <- "infercnv"
  pkg_ver <- as.character(utils::packageVersion(pkg_used))
  # window_size is the catalog-facing name; infercnv::run's argument is
  # window_length, and it is genuinely used there: run() hands it to
  # smooth_by_chromosome_runmeans(caTools::runmean), so the width changes the
  # smoothing. An even width has no centre gene, so it is rounded up and the
  # width that actually ran is what the metrics report.
  window_len <- if (!is.null(params$window_size)) as.integer(params$window_size) else 101L
  if (is.na(window_len) || window_len < 1L) window_len <- 101L
  if (window_len %% 2L == 0L) window_len <- window_len + 1L

  dir.create(outdir, recursive = TRUE, showWarnings = FALSE)
  gf_path <- file.path(outdir, "gene_order_tmp.txt")
  utils::write.table(go_sub, gf_path, sep = "\t", row.names = FALSE, col.names = FALSE, quote = FALSE)
  af_path <- file.path(outdir, "anno_tmp.txt")
  anno_df <- data.frame(cell = cell_names, group = anno, stringsAsFactors = FALSE)
  utils::write.table(anno_df, af_path, sep = "\t", row.names = FALSE, col.names = FALSE, quote = FALSE)

  infercnv_obj <- tryCatch({
    infercnv::CreateInfercnvObject(
      raw_counts_matrix = mat_sub,
      gene_order_file = gf_path,
      annotations_file = af_path,
      ref_group_names = ref_group,
      min_max_counts_per_cell = c(1, Inf),
      chr_exclude = c("chrX", "chrY", "chrM")
    )
  }, error = function(e) e)

  if (inherits(infercnv_obj, "error")) {
    unlink(c(gf_path, af_path))
    return(list(status = "failed", reason = paste("CreateInfercnvObject:", conditionMessage(infercnv_obj))))
  }

  internal_dir <- file.path(tempdir(), paste0("infercnv_", basename(outdir), "_", Sys.getpid()))
  dir.create(internal_dir, recursive = TRUE, showWarnings = FALSE)
  run_res <- tryCatch({
    infercnv::run(
      infercnv_obj,
      cutoff = if (use_counts) 0.1 else 1,
      out_dir = internal_dir,
      cluster_by_groups = TRUE,
      denoise = FALSE,
      HMM = FALSE,
      no_plot = TRUE,
      no_prelim_plot = TRUE,
      save_rds = FALSE,
      save_final_rds = FALSE,
      window_length = window_len,
      num_threads = 2
    )
  }, error = function(e) e)

  unlink(c(gf_path, af_path, internal_dir), recursive = TRUE)
  if (inherits(run_res, "error")) {
    return(list(status = "failed", reason = paste("infercnv::run:", conditionMessage(run_res))))
  }

  expr_res <- run_res@expr.data
  cells_out <- colnames(expr_res)
  cell_scores <- colMeans((expr_res - 1)^2)

  cell_df <- data.frame(
    cell = cells_out,
    group = anno[match(cells_out, cell_names)],
    cnv_score = as.numeric(cell_scores),
    is_reference = anno[match(cells_out, cell_names)] %in% ref_group,
    stringsAsFactors = FALSE
  )
  p1 <- sctda_write_csv(cell_df, file.path(outdir, "cnv_scores_per_cell.csv"))

  group_list <- split(cell_df, cell_df$group)
  grp_df <- do.call(rbind, lapply(names(group_list), function(gname) {
    sub <- group_list[[gname]]
    data.frame(
      group = gname,
      n_cells = nrow(sub),
      mean_cnv_score = mean(sub$cnv_score),
      median_cnv_score = stats::median(sub$cnv_score),
      is_reference = gname %in% ref_group,
      status = if (gname %in% ref_group) "reference" else "query",
      stringsAsFactors = FALSE
    )
  }))
  grp_df <- grp_df[order(-grp_df$mean_cnv_score), , drop = FALSE]
  p2 <- sctda_write_csv(grp_df, file.path(outdir, "cnv_summary_by_group.csv"))

  list(
    status = "succeeded",
    reason = sprintf("CNV inference completed with %s v%s against reference: %s",
                     pkg_used, pkg_ver, paste(ref_group, collapse=", ")),
    artifacts = c(p1, p2),
    metrics = list(
      package = pkg_used,
      version = pkg_ver,
      reference_group = paste(ref_group, collapse = ", "),
      n_reference_cells = n_ref_cells,
      n_query_cells = nrow(cell_df) - n_ref_cells,
      window_size = window_len,
      min_cells = min_cells
    )
  )
}

# ---------------------------------------------------------------- dispatch

SCTDA_ADAPTERS <- list(
  composition = task_composition,
  de_vs_rest_wilcox = task_de_vs_rest_wilcox,
  signature_score_aucell = task_signature_score_aucell,
  cnv_infercnv = task_cnv_infercnv
)
