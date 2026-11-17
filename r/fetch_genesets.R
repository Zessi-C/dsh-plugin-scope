#!/usr/bin/env Rscript
# Snapshot a public gene set collection to JSON, with provenance.
#
# The snapshot is what a protocol run actually uses: an online lookup would make
# a past run unreproducible the moment the collection is updated. The file
# records the msigdbr version, the collection id and the retrieval time so the
# report can state exactly which gene sets were scored.
#
# Usage: Rscript fetch_genesets.R --collection H --out DIR [--species "Homo sapiens"]

suppressPackageStartupMessages({
  library(jsonlite)
  library(msigdbr)
})

args <- commandArgs(trailingOnly = TRUE)
get_arg <- function(flag, default = NULL) {
  i <- match(flag, args); if (is.na(i) || i == length(args)) return(default); args[[i + 1]]
}
collection <- get_arg("--collection", "H")
species    <- get_arg("--species", "Homo sapiens")
outdir     <- get_arg("--out", ".")
dir.create(outdir, recursive = TRUE, showWarnings = FALSE)

d <- msigdbr(species = species, collection = collection)
if (nrow(d) == 0) {
  cat(toJSON(list(ok = FALSE, error = paste("no gene sets for collection", collection)),
             auto_unbox = TRUE))
  quit(status = 0)
}

sets <- split(unique(d[, c("gs_name", "gene_symbol")]), unique(d$gs_name))
sets <- lapply(sets, function(x) sort(unique(x$gene_symbol)))

out <- list(
  ok = TRUE,
  name = paste0("MSigDB ", collection),
  collection = collection,
  species = species,
  source = "msigdbr",
  source_version = as.character(packageVersion("msigdbr")),
  retrieved_utc = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
  n_sets = length(sets),
  n_unique_genes = length(unique(d$gene_symbol)),
  gene_sets = sets
)

path <- file.path(outdir, sprintf("genesets_%s_msigdbr%s.json", collection,
                                  gsub("\\.", "_", out$source_version)))
writeLines(toJSON(out, auto_unbox = TRUE, digits = NA), path)
cat(toJSON(list(ok = TRUE, path = path, n_sets = length(sets),
                source_version = out$source_version), auto_unbox = TRUE))
