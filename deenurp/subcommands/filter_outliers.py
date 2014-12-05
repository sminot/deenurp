"""
Filter sequences at the species level, whose distance from the species medoid
is above some threshold.
"""

import argparse
import csv
import logging
import os
import os.path
import sys
import shutil
import logging

from concurrent import futures
import numpy
import pandas as pd

from Bio import SeqIO
from taxtastic.taxtable import TaxNode

from .. import config, wrap, util, outliers

DEFAULT_RANK = 'species'
DEFAULT_ALIGNER = 'cmalign'
DEFAULT_MAXITERS = 2  # for muscle
DROP = 'drop'
KEEP = 'keep'
BLAST6NAMES = ['query', 'target', 'pct_id', 'align_len', 'mismatches', 'gaps',
               'qstart', 'qend', 'tstart', 'tend', 'evalue', 'bit_score']


def build_parser(p):
    p.add_argument('sequence_file', help="""All sequences""")
    p.add_argument('seqinfo_file', help="""Sequence info file""")
    p.add_argument(
        'taxonomy', help="""Taxtable""", type=argparse.FileType('r'))
    p.add_argument('output_fp', help="""Destination for sequences""",
                   type=argparse.FileType('w'))
    p.add_argument('--filter-rank', default=DEFAULT_RANK)
    p.add_argument('--filtered-seqinfo',
                   help="""Path to write filtered sequence info""",
                   type=argparse.FileType('w'))
    p.add_argument('--detailed-seqinfo',
                   help="""Sequence info, including filtering details""",
                   type=argparse.FileType('w'))
    p.add_argument('--log', help="""Log path""", type=argparse.FileType('w'))
    p.add_argument('--distance-cutoff', type=float, default=0.015,
                   help="""Distance cutoff from cluster centroid [default:
                   %(default)f]""")
    p.add_argument('--threads', type=int, default=config.DEFAULT_THREADS,
                   help="""number of taxa to process concurrently (one
                   process per multiple alignment)""")
    p.add_argument('--aligner', help='multiple alignment tool',
                   default=DEFAULT_ALIGNER,
                   choices=['cmalign', 'muscle', 'usearch'])
    p.add_argument('--executable',
                   help='Optional absolute or relative path to the alignment tool')
    p.add_argument('--maxiters', default=DEFAULT_MAXITERS, type=int,
                   help='Value for muscle -maxiters (ignored if using cmalign)')

    rare_group = p.add_argument_group("Rare taxa")
    rare_group.add_argument(
        '--min-seqs-for-filtering', type=int, default=5,
        help="""Minimum number of sequences perform distance-based
            medoid-filtering on [default: %(default)d]""")
    rare_group.add_argument(
        '--rare-taxon-action', choices=(KEEP, DROP), default=KEEP,
        help="""Action to perform when a taxon has <
            '--min-seqs-to-filter' representatives. [default: %(default)s]""")


def sequences_above_rank(taxonomy, rank=DEFAULT_RANK):
    """
    Generate the sequence ids from taxonomy whose rank is above specified rank.
    """
    ranks = taxonomy.ranks
    r_index = ranks.index(rank)
    assert r_index >= 0

    def above_rank(node):
        n_index = ranks.index(node.rank)
        assert n_index >= 0
        return n_index < r_index

    for n in taxonomy:
        if above_rank(n):
            for sequence_id in n.sequence_ids:
                yield sequence_id


def distmat_muscle(sequence_file, prefix, maxiters=DEFAULT_MAXITERS):

    with util.ntf(prefix=prefix, suffix='.fasta') as a_fasta:
        wrap.muscle_files(sequence_file, a_fasta.name, maxiters=maxiters)
        a_fasta.flush()

        taxa, distmat = outliers.fasttree_dists(a_fasta.name)

    return taxa, distmat


def distmat_cmalign(sequence_file, prefix):

    with util.ntf(prefix=prefix, suffix='.aln') as a_sto, \
            util.ntf(prefix=prefix, suffix='.fasta') as a_fasta:

        wrap.cmalign_files(sequence_file, a_sto.name, cpu=1)
        # FastTree requires FASTA
        SeqIO.convert(a_sto, 'stockholm', a_fasta, 'fasta')
        a_fasta.flush()

        taxa, distmat = outliers.fasttree_dists(a_fasta.name)

    return taxa, distmat


class UsearchError(Exception):
    pass


def parse_usearch_allpairs(filename, seqnames):
    """Read output of ``usearch -allpairs_global -blast6out`` and return a
    square distance matrix. ``seqnames`` determines the marginal order
    of sequences in the matrix.

    """

    data = pd.io.parsers.read_table(filename, header=None, names=BLAST6NAMES)
    data['dist'] = pd.Series(1.0 - data['pct_id'] / 100.0, index=data.index)

    # from IPython import embed; embed()

    # for each sequence pair, select the longest alignment if there is
    # more than one (chooses first occurrence if there are two the same length).
    maxidx = data.groupby(['query', 'target']).apply(lambda x: x['align_len'].idxmax())
    data = data.iloc[maxidx]

    if set(seqnames) != set(data['query']) | set(data['target']):
        raise UsearchError(
            'some sequences are missing from the output ({})'.format(filename))

    nseqs = len(seqnames)
    if (nseqs * (nseqs - 1)) / 2 != data.shape[0]:
        raise UsearchError(
            'not all pairwise comparisons are represented ({})'.format(filename))

    distmat = numpy.repeat(0.0, nseqs ** 2)
    distmat.shape = (nseqs, nseqs)
    ii = pd.match(data['query'], seqnames)
    jj = pd.match(data['target'], seqnames)
    distmat[ii, jj] = data['dist']
    distmat[jj, ii] = data['dist']

    return distmat


def distmat_usearch(sequence_file, prefix, usearch=wrap.USEARCH):

    with open(sequence_file) as sf, util.ntf(
            prefix=prefix, suffix='.blast6out') as uc:

        wrap.usearch_allpairs_files(sequence_file, uc.name, usearch)
        uc.flush()

        taxa = [seq.id for seq in SeqIO.parse(sf, 'fasta')]
        try:
            distmat = parse_usearch_allpairs(uc.name, taxa)
        except UsearchError, e:
            logging.error(e)
            shutil.copy(sequence_file, '.')
            raise UsearchError

    return taxa, distmat


def filter_sequences(sequence_file, tax_id, cutoff,
                     aligner=DEFAULT_ALIGNER,
                     maxiters=DEFAULT_MAXITERS,
                     usearch=wrap.USEARCH):
    """
    Return a list of sequence names identifying outliers.
    """

    assert aligner in {'cmalign', 'muscle', 'usearch'}

    prefix = '{}_'.format(tax_id)

    if aligner == 'cmalign':
        taxa, distmat = distmat_cmalign(sequence_file, prefix)
    elif aligner == 'muscle':
        taxa, distmat = distmat_muscle(sequence_file, prefix, maxiters)
    elif aligner == 'usearch':
        taxa, distmat = distmat_usearch(sequence_file, prefix, usearch)

    medoid, dists, is_out = outliers.outliers(distmat, cutoff)
    assert len(is_out) == len(taxa)

    result = pd.DataFrame({
        'seqname': taxa,
        'centroid': numpy.repeat(taxa[medoid], len(taxa)),
        'dist': dists,
        'is_out': is_out})

    return result


def mock_filter(seqs, keep):
    """Return a DataFrame with the same structure as the output of
    `filter_worker`. The value of the 'is_out' column is (not keep)
    for all seqs.

    """

    empty = numpy.repeat(numpy.nan, len(seqs))
    return pd.DataFrame({
        'seqname': seqs,
        'centroid': empty,
        'dist': empty,
        'is_out': numpy.repeat(not keep, len(seqs))})


def filter_worker(sequence_file, node, seqs, distance_cutoff,
                  aligner=DEFAULT_ALIGNER,
                  maxiters=DEFAULT_MAXITERS,
                  usearch=wrap.USEARCH,
                  log_taxid=None):
    """
    Worker task for running filtering tasks.

    Arguments:
    :sequence_file: Complete sequence file
    :node: Taxonomic node being filtered
    :seqs: set containing the names of sequences to keep
    :distance_cutoff: Distance cutoff for medoid filtering
    :sfetch_lock: Lock to acquire to retrieving sequences from ``sequence_file``
    :log_taxid: Optional function to log tax_id activity.

    :returns: Set of sequences to *keep*
    """

    prefix = '{}_'.format(node.tax_id)

    with util.ntf(prefix=prefix, suffix='.fasta') as tf:
        # Extract sequences
        wrap.esl_sfetch(sequence_file, seqs, tf)
        tf.flush()

        filtered = filter_sequences(tf.name, node.tax_id, distance_cutoff,
                                    aligner=aligner, maxiters=maxiters)

        if log_taxid:
            log_taxid(node.tax_id, node.name, len(seqs),
                      sum(~filtered.is_out), sum(filtered.is_out))

        return filtered


def action(a):
    # remove .ssi index for sequence file if it exists
    try:
        os.remove(a.sequence_file + '.ssi')
    except OSError:
        pass

    # Load taxonomy
    with a.taxonomy as fp:
        taxonomy = TaxNode.from_taxtable(fp)
        logging.info('Loaded taxonomy')

    # Load sequences into taxonomy
    with open(a.seqinfo_file) as fp:
        taxonomy.populate_from_seqinfo(fp)
        logging.info('Added %d sequences', sum(1 for i in taxonomy.subtree_sequence_ids()))

    outcomes = []  # accumulate DatFrame objects

    log_taxid = None
    if a.log:
        writer = csv.writer(a.log, lineterminator='\n',
                            quoting=csv.QUOTE_NONNUMERIC)
        writer.writerow(('tax_id', 'tax_name', 'n', 'kept', 'pruned'))

        def log_taxid(tax_id, tax_name, n, kept, pruned):
            writer.writerow((tax_id, tax_name, n, kept, pruned))

    filter_rank_col = '{}_id'.format(a.filter_rank)

    with a.log or util.nothing():
        # Sequences which are classified above the desired rank should just be kept
        names_above_rank = list(sequences_above_rank(taxonomy, a.filter_rank))
        logging.info('Keeping %d sequences classified above %s',
                     len(names_above_rank), a.filter_rank)
        above_rank = mock_filter(names_above_rank, keep=True)
        above_rank[filter_rank_col] = pd.Series(numpy.nan, index=above_rank.index)
        outcomes.append(above_rank)

        # For each filter-rank, filter
        nodes = [i for i in taxonomy if i.rank == a.filter_rank]

        # Filter each tax_id, running in ``--threads`` tasks in parallel
        with futures.ThreadPoolExecutor(a.threads) as executor:

            # dispatch a pool of tasks
            futs = {}
            for i, node in enumerate(nodes):
                seqs = frozenset(node.subtree_sequence_ids())

                if not seqs:
                    logging.debug("No sequences for %s (%s)", node.tax_id, node.name)
                    if log_taxid:
                        log_taxid(node.tax_id, node.name, 0, 0, 0)
                    continue
                elif len(seqs) < a.min_seqs_for_filtering:
                    logging.debug('%d sequence(s) for %s (%s) [action: %s]',
                                  len(seqs), node.tax_id, node.name,
                                  a.rare_taxon_action)
                    if a.rare_taxon_action == DROP:
                        f = executor.submit(mock_filter, seqs=list(seqs), keep=False)
                    elif a.rare_taxon_action == KEEP:
                        f = executor.submit(mock_filter, seqs=list(seqs), keep=True)
                    else:
                        raise ValueError("Unknown action: {0}".format(
                            a.rare_taxon_action))
                else:
                    # `f` is a DataFrame (output of filter_sequences)
                    f = executor.submit(filter_worker,
                                        sequence_file=a.sequence_file,
                                        node=node,
                                        seqs=seqs,
                                        distance_cutoff=a.distance_cutoff,
                                        aligner=a.aligner,
                                        maxiters=a.maxiters,
                                        log_taxid=log_taxid)

                futs[f] = {'n_seqs': len(seqs), 'node': node}

            # log results for each tax_id as tasks complete
            complete = 0
            while futs:
                done, pending = futures.wait(futs, 1, futures.FIRST_COMPLETED)
                complete += len(done)
                sys.stderr.write('{0:8d}/{1:8d} taxa completed\r'.format(
                    complete, complete + len(pending)))
                for f in done:
                    if f.exception():
                        logging.exception("Error in child process: %s", f.exception())
                        executor.shutdown(False)
                        raise f.exception()

                    info = futs.pop(f)
                    filtered = f.result()  # here's the DataFrame again...

                    # add a column for tax_d at filter_rank
                    filtered[filter_rank_col] = pd.Series(
                        info['node'].tax_id, index=filtered.index)
                    outcomes.append(filtered)

                    kept = frozenset(filtered.seqname[~filtered.is_out])
                    if len(kept) == 0:
                        logging.info('Pruned all %d sequences for %s (%s)',
                                     info['n_seqs'], info['node'].tax_id,
                                     info['node'].name)
                    elif len(kept) != info['n_seqs']:
                        logging.info('Pruned %d/%d sequences for %s (%s)',
                                     info['n_seqs'] - len(kept), info['n_seqs'],
                                     info['node'].tax_id, info['node'].name)

    all_outcomes = pd.concat(outcomes, ignore_index=True)
    all_outcomes.set_index('seqname', inplace=True)

    # all input sequences should be in the output
    assert {s for node in taxonomy for s in node.sequence_ids} == set(all_outcomes.index)

    kept_ids = set(all_outcomes.index[~all_outcomes.is_out])

    with a.output_fp as fp:
        # Extract all of the sequences that passed.
        logging.info('Extracting %d sequences', len(kept_ids))
        wrap.esl_sfetch(a.sequence_file, kept_ids, fp)

    # Filter seqinfo for sequences that passed.
    seqinfo = pd.io.parsers.read_csv(
        a.seqinfo_file, dtype={'seqname': str, 'tax_id': str})
    seqinfo.set_index('seqname', inplace=True)

    merged = seqinfo.join(all_outcomes, lsuffix='.left')

    # csv output
    if a.filtered_seqinfo:
        merged[~merged.is_out].to_csv(a.filtered_seqinfo, columns=seqinfo.columns)

    if a.detailed_seqinfo:
        merged.to_csv(a.detailed_seqinfo)
