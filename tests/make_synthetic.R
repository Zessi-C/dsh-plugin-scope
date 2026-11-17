#!/usr/bin/env Rscript
# Build a small synthetic Seurat object for software tests.
#
# Two groups (ctrl/treat) x three samples x two cell types, with a real counts
# layer so pseudobulk and AUCell exercise the same code path as a real object.
# This object exists only to test software behaviour; it is not a biological
# reference and no result computed from it means anything.
suppressPackageStartupMessages({library(Seurat); library(Matrix)})

args <- commandArgs(trailingOnly = TRUE)
out <- if (length(args) >= 1) args[[1]] else "tests/data/synthetic.rds"
dir.create(dirname(out), recursive = TRUE, showWarnings = FALSE)

set.seed(42)
n_genes <- 400
genes <- paste0("GENE", seq_len(n_genes))
genes[1:5] <- c("ACKR1", "MMRN1", "SEMA3G", "GJA4", "FLT1")

samples <- c("S1", "S2", "S3", "S4", "S5", "S6")
conds <- c(ctrl = "S1", ctrl = "S2", ctrl = "S3", treat = "S4", treat = "S5", treat = "S6")
cond_of <- c(S1 = "ctrl", S2 = "ctrl", S3 = "ctrl", S4 = "treat", S5 = "treat", S6 = "treat")
celltypes <- c("Alpha", "Beta")

cells <- list()
counts_list <- list()
meta_rows <- list()
idx <- 0
for (s in samples) {
  for (ct in celltypes) {
    n <- if (ct == "Alpha") 40 else 25          # uneven groups on purpose
    idx <- idx + 1
    bcs <- paste0(s, "_", ct, "_", seq_len(n))
    mu <- runif(n_genes, 0.2, 3)
    if (ct == "Alpha") mu[1:3] <- mu[1:3] * 6   # marker-like signal
    if (cond_of[[s]] == "treat" && ct == "Alpha") mu[4:5] <- mu[4:5] * 4
    m <- matrix(rpois(n_genes * n, lambda = rep(mu, n)), nrow = n_genes,
                dimnames = list(genes, bcs))
    counts_list[[idx]] <- as(m, "CsparseMatrix")
    meta_rows[[idx]] <- data.frame(
      cell = bcs, orig.ident = s, group = cond_of[[s]], celltype = ct,
      unlabeled_probe = ifelse(seq_len(n) %% 20 == 0, NA_character_, ct),
      stringsAsFactors = FALSE)
  }
}
mat <- do.call(cbind, counts_list)
meta <- do.call(rbind, meta_rows)
rownames(meta) <- meta$cell
obj <- CreateSeuratObject(counts = mat, meta.data = meta, project = "synthetic")
obj[["percent.mt"]] <- 0
obj <- NormalizeData(obj, verbose = FALSE)
obj <- FindVariableFeatures(obj, nfeatures = 200, verbose = FALSE)
obj <- ScaleData(obj, verbose = FALSE)
obj <- RunPCA(obj, npcs = 10, verbose = FALSE)
saveRDS(obj, out)
cat(sprintf("wrote %s: %d genes x %d cells\n", out, nrow(obj), ncol(obj)))
