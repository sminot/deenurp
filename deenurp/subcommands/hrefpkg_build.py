"""
Build a hierarchical set of reference packages.

Chooses 5 sequences for each species; or next rank up if species-level sequences are not available.
"""

import csv
import itertools
import logging
import os.path
import random
import sys
import tempfile


from Bio import SeqIO
from taxtastic.refpkg import Refpkg

from .. import wrap, tax

def comma_set(s):
    s = s.split(',')
    s = frozenset(i.strip() for i in s)
    return s

def build_parser(p):
    p.add_argument('sequence_file', help="""All sequences""")
    p.add_argument('seqinfo_file', help="""Sequence info file""")
    p.add_argument('taxonomy', help="""Taxtable""")
    p.add_argument('--index-rank', help="""Rank for individual reference
            packages [default: %(default)s]""", default='order')
    p.add_argument('--threads', type=int, default=12, help="""Number of threads
            [default: %(default)d]""")
    p.add_argument('--only', help="""List of taxids to keep""", type=comma_set)
    p.add_argument('--seed', type=int, default=1)

def action(a):
    random.seed(a.seed)
    if os.path.exists('index.refpkg'):
        raise IOError('index.refpkg exists.')

    with open(a.taxonomy) as fp:
        logging.info('loading taxonomy')
        taxonomy = tax.TaxNode.from_taxtable(fp)

    with open(a.seqinfo_file) as fp:
        logging.info("loading seqinfo")
        seqinfo = load_seqinfo(fp)

    nodes = [i for i in taxonomy if i.rank == a.index_rank]
    hrefpkgs = []
    with open('index.csv', 'w') as fp:
        def log_hrefpkg(tax_id):
            path = tax_id + '.refpkg'
            fp.write('{0},{0}.refpkg\n'.format(tax_id))
            hrefpkgs.append(path)

        for i, node in enumerate(nodes):
            if a.only and node.tax_id not in a.only:
                logging.info("Skipping %s", node.tax_id)
                continue

            logging.info("%s: %s (%d/%d)", node.tax_id, node.name, i+1, len(nodes))
            if os.path.exists(node.tax_id + '.refpkg'):
                logging.warn("Refpkg exists: %s.refpkg. Skipping", node.tax_id)
                log_hrefpkg(node.tax_id)
                continue
            r = tax_id_refpkg(node.tax_id, taxonomy, seqinfo, a.sequence_file)
            if r:
                log_hrefpkg(node.tax_id)

        # Build index refpkg
        logging.info("Building index.refpkg")
        index_rp, sequence_ids = build_index_refpkg(hrefpkgs, a.sequence_file,
                seqinfo, a.taxonomy, index_rank=a.index_rank)

        # Write unused seqs
        logging.info("Extracting unused sequences")
        seqs = (i for i in SeqIO.parse(a.sequence_file, 'fasta')
                if i.id not in sequence_ids)
        c = SeqIO.write(seqs, 'not_in_hrefpkgs.fasta', 'fasta')
        logging.info("%d sequences not in hrefpkgs.", c)

def find_nodes(taxonomy, index_rank, want_rank='species'):
    """
    Find nodes to select sequences from, preferring want_rank-level nodes, but
    moving up a rank if no species-level nodes with sequences exist.
    """
    ranks = taxonomy.ranks
    rdict = dict(zip(ranks, xrange(len(ranks))))
    assert index_rank in rdict
    assert want_rank in rdict
    def any_sequences_below(node):
        for i in node:
            if i.sequence_ids:
                return True
        return False
    def try_next(it):
        try:
            return next(it)
        except StopIteration:
            return None
    def inner(node):
        # Prefer nodes at want_rank with sequences
        if node.rank == want_rank and any_sequences_below(node):
            yield node
        else:
            nodes_below = itertools.chain.from_iterable(inner(i) for i in node.children)
            first = try_next(nodes_below)
            if first:
                # If child taxa have valid sequences, use them
                for i in itertools.chain([first], nodes_below):
                    yield i
            else:
                # If there are sequences here, and it's not a made up rank
                # ('below_genus, etc), and the rank is more specific than the
                # index rank, include sequences from the node.
                if (any_sequences_below(node) and 'below' not in node.rank and
                        rdict[index_rank] < rdict[node.rank]):
                    yield node
    return inner(taxonomy)

def load_seqinfo(seqinfo_fp):
    r = csv.DictReader(seqinfo_fp)
    return list(r)

def build_index_refpkg(hrefpkg_names, sequence_file, seqinfo, taxonomy, dest='index.refpkg', **meta):
    """
    Build an index.refpkg from a set of hrefpkgs
    """
    def sequence_names(f):
        with open(f) as fp:
            r = csv.DictReader(fp)
            for i in r:
                yield i['seqname']

    hrefpkgs = (Refpkg(i, create=False) for i in hrefpkg_names)
    seqinfo_files = (i.file_abspath('seq_info') for i in hrefpkgs)
    sequence_ids = frozenset(i for f in seqinfo_files
                             for i in sequence_names(f))

    with wrap.ntf(prefix='aln_fasta', suffix='.fasta') as tf, \
         wrap.ntf(prefix='seq_info', suffix='.csv') as seq_info_fp:
        wrap.esl_sfetch(sequence_file, sequence_ids, tf)
        tf.close()

        # Seqinfo file
        r = (i for i in seqinfo if i['seqname'] in sequence_ids)
        w = csv.DictWriter(seq_info_fp, seqinfo[0].keys(), lineterminator='\n',
                quoting=csv.QUOTE_NONNUMERIC)
        w.writeheader()
        w.writerows(r)
        seq_info_fp.close()

        rp = Refpkg(dest, create=True)
        rp.start_transaction()
        rp.update_file('aln_fasta', tf.name)
        rp.update_file('seq_info', seq_info_fp.name)
        rp.update_file('taxonomy', taxonomy)

        for k, v in meta.items():
            rp.update_metadata(k, v)

        rp.commit_transaction()

    return rp, sequence_ids

def choose_sequence_ids(taxonomy, seqinfo_rows, per_taxon=5, index_rank='order'):
    """
    Select sequences
    """
    for i in seqinfo_rows:
        taxonomy.get_node(i['tax_id']).sequence_ids.append(i['seqname'])

    nodes = find_nodes(taxonomy, index_rank)
    for node in nodes:
        node_seqs = list(node.subtree_sequence_ids())
        if len(node_seqs) > per_taxon:
            node_seqs = random.sample(node_seqs, per_taxon)
        for i in node_seqs:
            yield i

def tax_id_refpkg(tax_id, full_tax, seqinfo, sequence_file, threads=12, index_rank='order'):
    """
    Build a reference package containing all descendants of tax_id from an
    index reference package.
    """
    with wrap.ntf(prefix='taxonomy', suffix='.csv') as tax_fp, \
         wrap.ntf(prefix='aln_sto', suffix='.sto') as sto_fp, \
         wrap.ntf(prefix='tree', suffix='.tre') as tree_fp, \
         wrap.ntf(prefix='tree', suffix='.stats') as stats_fp, \
         wrap.ntf(prefix='seq_info', suffix='.csv') as seq_info_fp:

        # Subset taxonomy
        n = full_tax.get_node(tax_id)
        descendants = set(i.tax_id for i in n)
        assert descendants
        n.write_taxtable(tax_fp)
        tax_fp.close()

        # Subset seq_info
        w = csv.DictWriter(seq_info_fp, seqinfo[0].keys(),
                quoting=csv.QUOTE_NONNUMERIC)
        w.writeheader()
        rows = [i for i in seqinfo if i['tax_id'] in descendants]
        sinfo = {i['seqname']: i for i in rows}
        keep_seq_ids = frozenset(choose_sequence_ids(n, rows,
                                 index_rank=index_rank))
        rows = [sinfo[i] for i in keep_seq_ids]
        w.writerows(rows)
        seq_info_fp.close()

        # Fetch sequences
        with tempfile.NamedTemporaryFile() as tf:
            wrap.esl_sfetch(sequence_file,
                            keep_seq_ids, tf)
            # Rewind
            tf.seek(0)
            sequences = list(SeqIO.parse(tf, 'fasta'))
        logging.info("Tax id %s: %d sequences", tax_id, len(sequences))

        if len(set(str(i.seq) for i in sequences)) == 1:
            logging.warn("Skipping %s: only 1 unique sequence string", tax_id)
            return None

        # No sense in building with one sequence
        if len(sequences) < 2:
            logging.warn("Skipping: %d sequences.", len(sequences))
            return None

        # Cmalign
        aligned = wrap.cmalign(sequences, output=sto_fp.name, mpi_args=['-np', str(threads)])
        # Tree
        wrap.fasttree(aligned, stats_fp.name, tree_fp, threads=threads, gtr=True)
        tree_fp.close()
        sto_fp.close()

        rp = Refpkg(tax_id + '.refpkg')
        rp.start_transaction()
        rp.update_file('aln_sto', sto_fp.name)
        rp.update_file('tree', tree_fp.name)
        rp.update_file('seq_info', seq_info_fp.name)
        rp.update_file('taxonomy', tax_fp.name)
        try:
            rp.update_phylo_model('FastTree', stats_fp.name)
        except:
            print >> sys.stderr, stats_fp.read()
            raise
        rp.update_file('profile', wrap.CM)
        rp.commit_transaction()

        return rp