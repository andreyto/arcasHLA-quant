#!/usr/bin/env python
# -*- coding: utf-8 -*-

#-------------------------------------------------------------------------------
#   align.py: alignment functions for genotyping.
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
#   This file is part of arcasHLA.
#
#   arcasHLA is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   arcasHLA is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with arcasHLA.  If not, see <https://www.gnu.org/licenses/>.
#-------------------------------------------------------------------------------

import os
import sys
import re
import json
import pickle
import argparse
import logging as log

import numpy as np
import math
import pandas as pd

from datetime import date
from argparse import RawTextHelpFormatter
from textwrap import wrap
from collections import Counter, defaultdict
from itertools import combinations

from reference import check_ref, get_exon_combinations
from arcas_utilities import *

__version__     = '0.2.0'
__date__        = '2019-06-26'

#-------------------------------------------------------------------------------
#   Paths and filenames
#-------------------------------------------------------------------------------

rootDir = os.path.dirname(os.path.realpath(__file__)) + '/../'

#-----------------------------------------------------------------------------
# Process and align FASTQ input
#-----------------------------------------------------------------------------

def analyze_reads(fqs, paired, reads_file):
    '''Analyzes read length for single-end sampled, required by Kallisto.'''
    
    awk = "| awk '{if(NR%4==2) print length($1)}'"
    
    if fqs[0].endswith('.gz'):
        cat = 'zcat'
    else:
        cat = 'cat'
    
    log.info('[alignment] Analyzing read length')
    if paired:
        fq1, fq2 = fqs
        run_command([cat, '<', fq1, awk, '>' , reads_file])
        run_command([cat, '<', fq2, awk, '>>', reads_file])
        
    else:
        fq = fqs[0]
        run_command([cat, '<', fq, awk, '>', reads_file])
        
    read_lengths = np.genfromtxt(reads_file)
    
    if len(read_lengths) == 0:
        sys.exit('[genotype] Error: FASTQ files are empty; check arcasHLA extract for issues.')
    
    num = len(read_lengths)
    avg = round(np.mean(read_lengths), 6)
    std = round(np.std(read_lengths), 6)
    
    return num, avg, std

def pseudoalign(fqs, sample, paired, reference, outdir, temp, threads):
    '''Calls Kallisto to pseudoalign reads.'''
    
    # Get read length stats
    reads_file = ''.join([temp, sample, '.reads.txt'])
    num, avg, std = analyze_reads(fqs, paired, reads_file)
    
    # Kallisto fails if std used for single-end is 0
    std = max(std, 1e-6)

    command = ['kallisto pseudo -i', reference, '-t', threads, '-o', temp]
        
    if paired:
        command.extend([fqs[0], fqs[1]])
    else:
        fq = fqs[0]
        command.extend(['--single -l', str(avg), '-s', str(std), fq])
        
    run_command(command, '[alignment] Pseudoaligning with Kallisto: ')
           
    return num, avg, std

#-----------------------------------------------------------------------------
# Process transcript assembly output
#-----------------------------------------------------------------------------

def process_counts(count_file, eq_file, gene_list, allele_idx, allele_lengths): 
    '''Processes pseudoalignment output, returning compatibility classes.'''
    log.info('[alignment] Processing pseudoalignment')
    # Process count information
    counts = dict()
    with open(count_file, 'r', encoding='UTF-8') as file:
        for line in file.read().splitlines():
            eq, count = line.split('\t')
            counts[eq] = float(count)

    
    # Process compatibility classes
    eqs = dict()
    with open(eq_file, 'r', encoding='UTF-8') as file:
        for line in file.read().splitlines():
            eq, indices = line.split('\t')
            eqs[eq] = indices.split(',')

    # Set up compatibility class index
    eq_idx = defaultdict(list)
    
    count_unique = 0
    count_multi = 0
    
    for eq, indices in eqs.items():
        if [idx for idx in indices if not allele_idx[idx]]:
            continue

        genes = list({get_gene(allele) for idx in indices 
                        for allele in allele_idx[idx]})
        count = counts[eq]
        
        if len(genes) == 1 and counts[eq] > 0:
            gene = genes[0]
            eq_idx[gene].append((indices, count))
            
            count_unique += count
        else:
            count_multi += count

    # Alleles mapping to their respective compatibility classes
    allele_eq = defaultdict(set)
    for eqs in eq_idx.values():
        for eq,(indices,_) in enumerate(eqs):
            for idx in indices:
                allele_eq[idx].add(eq)
    
    return eq_idx, allele_eq, [count_unique, count_multi]

def process_partial_counts(count_file, eq_file, allele_idx, allele_lengths, 
                           exon_idx, exon_combos):
    '''Processes pseudoalignment output, returning compatibility classes.'''
    
    log.info('[alignment] Processing pseudoalignment')
    counts_index = dict()
    with open(count_file,'r', encoding='UTF-8') as file:
        for line in file.read().splitlines():
            eq, count = line.split('\t')
            counts_index[eq] = float(count)

    eqs = dict()
    with open(eq_file,'r', encoding='UTF-8') as file:
        for line in file.read().splitlines():
            eq, indices = line.split('\t')
            eqs[eq] = indices.split(',')

    eq_idx = {str(i):defaultdict(list) for i in exon_combos}
    count_unique = 0
    count_multi = 0

    for eq, indices in eqs.items():
        if [index for index in indices if not allele_idx[index]]:
            continue

        genes = list({allele.split('*')[0] for index in indices 
                      for allele in allele_idx[index]})
        
        count = counts_index[eq]
                      
        exons = list({exon_idx[index] for index in indices})
        if len(genes) == 1 and  count > 0:
            gene = genes[0]
            for exon in exons:
                exon_indices = list({index for index in indices 
                                      if exon_idx[index] == exon})
                                     
                eq_idx[exon][gene].append((exon_indices, count))
            count_unique += count
        else:
            count_multi += count
                
    return eq_idx, [count_unique, count_multi]
           

def get_count_stats(eq_idx, gene_length):
    '''Returns counts and relative abundance of genes.'''
    stats = {gene:[0,0,0.] for gene in eq_idx}
    
    abundances = defaultdict(float)
    for gene, eqs in eq_idx.items():
        count = sum([count for eq,count in eqs])
        abundances[gene] = count / gene_length[gene]
        stats[gene][0] = count
        stats[gene][1] = len(eqs)
        
    total_abundance = sum(abundances.values())

    for gene, abundance in abundances.items():
        stats[gene][2] = abundance / total_abundance

    return stats

def alignment_summary(align_stats, partial = False):
    '''Prints alignment summary to log.'''
    count_unique, count_multi, total, _, _ = align_stats
    log.info('[alignment] Processed {:.0f} reads, {:.0f} pseudoaligned '
             .format(total, count_unique + count_multi)+
             'to HLA reference')
              
    log.info('[alignment] {:.0f} reads mapped to a single HLA gene'
             .format(count_unique))

def gene_summary(gene_stats):
    '''Prints gene read count and relative abundance to log.'''

    log.info('[alignment] Observed HLA genes:')

    log.info('\t\t{: <10}    {}    {}    {}'
             .format('gene','abundance','read count','classes'))

    for g,(c,e,a) in sorted(gene_stats.items()):
        log.info('\t\tHLA-{: <6}    {: >8.2f}%    {: >10.0f}    {: >7.0f}'
                 .format(g, a*100, c, e))

def get_alignment(fqs, sample, reference, reference_info, outdir, temp, threads, partial = False):
    '''Runs pseudoalignment and processes output.'''
    paired = True if len(fqs) == 2 else False
        
    count_file = ''.join([temp, 'pseudoalignments.tsv'])
    eq_file = ''.join([temp, 'pseudoalignments.ec'])

    total, avg, std = pseudoalign(fqs,
                                 sample,
                                 paired,
                                 reference,
                                 outdir, 
                                 temp,
                                 threads)
    
    # Process partial genotyping pseudoalignment
    if partial:
        (commithash, (gene_set, allele_idx, exon_idx, 
            lengths, partial_exons, partial_alleles)) = reference_info
        
        exon_combos = get_exon_combinations() 
        
        eq_idx, align_stats = process_partial_counts(count_file,
                                               eq_file,
                                               allele_idx, 
                                               lengths,
                                               exon_idx,
                                               exon_combos)
        align_stats.extend([total, avg, std])
        
        alignment_summary(align_stats, True)
        
        with open(''.join([outdir,sample,'.partial_alignment.p']),'wb') as file:
            alignment_info = [commithash, eq_idx, [], paired, 
                              align_stats, []]
            pickle.dump(alignment_info, file)
            
    # Process regular pseudoalignment
    else:
        (commithash,(gene_set, allele_idx, 
             lengths, gene_length)) = reference_info
        
        eq_idx, allele_eq, align_stats = process_counts(count_file,
                                                        eq_file, 
                                                        gene_set, 
                                                        allele_idx, 
                                                        lengths)
        
        align_stats.extend([total, avg, std])
        
        alignment_summary(align_stats)
        
        gene_stats = get_count_stats(eq_idx, gene_length)
        gene_summary(gene_stats)

        with open(''.join([outdir, sample, '.alignment.p']), 'wb') as file:
            alignment_info = [commithash, eq_idx, allele_eq, paired, 
                              align_stats, gene_stats]
            pickle.dump(alignment_info, file)
            
        with open(''.join([outdir,sample,'.genes.json']), 'w') as file:
            json.dump(gene_stats, file)
            
    return alignment_info

def load_alignment(file, commithash, partial = False):
    '''Loads previous pseudoalignment.'''
    
    log.info(f'[alignment] Loading previous alignment %s', file)
    
    with open(file, 'rb') as file:
        alignment_info = pickle.load(file)
    
    # Compatibility with arcasHLA 1.0
    if len(alignment_info) != 5:
        if partial:
            (commithash_alignment, eq_idx, paired,
                 _, _, _, _) = alignment_info
            alignment_info = [commithash, eq_idx, None, paired, 
                              None, None]
        else:
            (commithash_alignment, eq_idx, allele_eq, paired, 
                 align_stats, gene_stats, num, avg, std) = alignment_info
            align_stats = align_stats.extend([num, avg, std])
            alignment_info = [commithash, eq_idx, allele_eq, paired, 
                              align_stats, gene_stats]
    
    commithash_alignment, _,_,_, align_stats, gene_stats = alignment_info
        
    if commithash != commithash_alignment:
        sys.exit('[alignment] Error: reference used for alignment ' +
                 'different than the one in the database')
        
    if align_stats: alignment_summary(align_stats)
    if not partial: gene_summary(gene_stats)
        
    return alignment_info

#-----------------------------------------------------------------------------
