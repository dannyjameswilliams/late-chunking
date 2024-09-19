import logging
from typing import Any, Optional

import numpy as np
import torch
from mteb.abstasks import AbsTask
from mteb.evaluation.evaluators import RetrievalEvaluator
from mteb.tasks import Retrieval
from tqdm import tqdm
from mteb.load_results.mteb_results import ScoresDict

from chunked_pooling import chunked_pooling
from chunked_pooling.chunking import Chunker

logger = logging.getLogger(__name__)


class AbsTaskChunkedRetrieval(AbsTask):
    def __init__(
        self,
        chunking_strategy: str = None,
        chunked_pooling_enabled: bool = False,
        tokenizer: Optional[Any] = None,
        prune_size: Optional[int] = None,
        chunk_size: Optional[int] = None,
        n_sentences: Optional[int] = None,
        model_has_instructions: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        try:
            self.retrieval_task = getattr(
                Retrieval,
                self.metadata_dict['dataset'].get('name', None)
                or self.metadata_dict.get('name'),
            )()
        except:
            logger.warning('Could not initialize retrieval_task')
        self.chunking_strategy = chunking_strategy
        self.chunker = Chunker(self.chunking_strategy)
        self.chunked_pooling_enabled = chunked_pooling_enabled
        self.tokenizer = tokenizer
        self.prune_size = prune_size
        self.model_has_instructions = model_has_instructions
        self.chunking_args = {
            'chunk_size': chunk_size,
            'n_sentences': n_sentences,
        }

    def load_data(self, **kwargs):
        self.retrieval_task.load_data(**kwargs)
        self.corpus = self.retrieval_task.corpus
        self.queries = self.retrieval_task.queries
        self.relevant_docs = self.retrieval_task.relevant_docs
        # prune dataset
        if self.prune_size:
            self.queries, self.corpus, self.relevant_docs = self._prune(
                self.queries, self.corpus, self.relevant_docs, self.prune_size
            )

    def calculate_metadata_metrics(self):
        self.retrieval_task.calculate_metadata_metrics()

    def evaluate(
        self, model, split: str = "test", encode_kwargs: dict[str, Any] = {}, **kwargs
    ) -> dict[str, ScoresDict]:
        scores: dict[str, ScoresDict] = {}
        hf_subsets = list(self.hf_subsets) if self.is_multilingual else ["default"]

        for hf_subset in hf_subsets:
            logger.info(f"Subset: {hf_subset}")

            if hf_subset == "default":
                corpus, queries, relevant_docs = (
                    self.corpus[split],
                    self.queries[split],
                    self.relevant_docs[split],
                )
            else:
                corpus, queries, relevant_docs = (
                    self.corpus[hf_subset][split],
                    self.queries[hf_subset][split],
                    self.relevant_docs[hf_subset][split],
                )

            scores[hf_subset] = self._evaluate_monolingual(
                model, corpus, queries, relevant_docs, hf_subset, **kwargs
            )

        return scores

    def _evaluate_monolingual(
        self, model, corpus, queries, relevant_docs, lang=None, batch_size=1, **kwargs
    ):
        # split corpus into chunks
        if not self.chunked_pooling_enabled:
            corpus = self._apply_chunking(corpus, self.tokenizer)
            max_chunks = max([len(x) for x in corpus.values()])
            corpus = self._flatten_chunks(corpus)
            k_values = self._calculate_k_values(max_chunks)
            # determine the maximum number of documents to consider in a ranking
            max_k = int(max(k_values) / max_chunks)
            retriever = RetrievalEvaluator(
                model, k_values=k_values, batch_size=batch_size, **kwargs
            )
            results = retriever(corpus, queries)
        else:
            query_ids = list(queries.keys())
            query_texts = [queries[k] for k in query_ids]
            if hasattr(model, 'encode_queries'):
                query_embs = model.encode_queries(query_texts)
            else:
                query_embs = model.encode(query_texts)

            corpus_ids = list(corpus.keys())
            corpus_texts = [
                (
                    f"{corpus[k]['title']} {corpus[k]['text']}"
                    if 'title' in corpus[k]
                    else corpus[k]['text']
                )
                for k in corpus_ids
            ]

            chunk_annotations = self._calculate_annotations(model, corpus_texts)

            corpus_embs = []
            with torch.no_grad():
                for inputs in tqdm(
                    self._batch_inputs(
                        list(zip(corpus_texts, chunk_annotations)),
                        batch_size=batch_size,
                    ),
                    total=(len(corpus_texts) // batch_size),
                ):
                    if self.model_has_instructions:
                        instr = model.get_instructions()[1]
                    else:
                        instr = ''
                    text_inputs = [instr + x[0] for x in inputs]
                    annotations = [x[1] for x in inputs]
                    model_inputs = self.tokenizer(
                        text_inputs,
                        return_tensors='pt',
                        padding=True,
                        truncation=True,
                        max_length=8192,
                    )
                    if model.device.type == 'cuda':
                        model_inputs = {
                            k: v.to(model.device) for k, v in model_inputs.items()
                        }
                    model_outputs = model(**model_inputs)
                    output_embs = chunked_pooling(
                        model_outputs, annotations, max_length=8192
                    )
                    corpus_embs.extend(output_embs)

            max_chunks = max([len(x) for x in corpus_embs])
            k_values = self._calculate_k_values(max_chunks)
            # determine the maximum number of documents to consider in a ranking
            max_k = int(max(k_values) / max_chunks)
            (
                chunk_id_list,
                doc_to_chunk,
                flattened_corpus_embs,
            ) = self.flatten_corpus_embs(corpus_embs, corpus_ids)
            similarity_matrix = np.dot(query_embs, flattened_corpus_embs.T)
            results = self.get_results(
                chunk_id_list, k_values, query_ids, similarity_matrix
            )

        doc_results = self.get_doc_results(results)

        ndcg, _map, recall, precision, _ = RetrievalEvaluator.evaluate(
            relevant_docs,
            doc_results,
            [k for k in k_values if k <= max_k],
            ignore_identical_ids=kwargs.get('ignore_identical_ids', True),
        )
        mrr, _ = RetrievalEvaluator.evaluate_custom(
            relevant_docs,
            doc_results,
            [k for k in k_values if k <= max_k],
            'mrr',
        )
        scores = {
            **{f"ndcg_at_{k.split('@')[1]}": v for (k, v) in ndcg.items()},
            **{f"map_at_{k.split('@')[1]}": v for (k, v) in _map.items()},
            **{f"recall_at_{k.split('@')[1]}": v for (k, v) in recall.items()},
            **{f"precision_at_{k.split('@')[1]}": v for (k, v) in precision.items()},
            **{f"mrr_at_{k.split('@')[1]}": v for (k, v) in mrr.items()},
        }
        self._add_main_score(scores)
        return scores

    def _add_main_score(self, scores: ScoresDict) -> None:
        scores["main_score"] = scores[self.metadata.main_score]

    def get_results(self, chunk_id_list, k_values, query_ids, similarity_matrix):
        results = {}
        for i, query_id in enumerate(query_ids):
            query_results = {}
            for idx, score in enumerate(similarity_matrix[i]):
                chunk_id = chunk_id_list[idx]
                query_results[chunk_id] = score
            # Sort results by score and only keep the top k scores
            sorted_query_results = dict(
                sorted(query_results.items(), key=lambda item: item[1], reverse=True)[
                    : max(k_values)
                ]
            )
            results[query_id] = sorted_query_results
        return results

    def flatten_corpus_embs(self, corpus_embs, corpus_ids):
        doc_to_chunk = {}
        flattened_corpus_embs = []
        chunk_id_list = []
        for doc_id, emb in zip(corpus_ids, corpus_embs):
            for i, chunk in enumerate(emb):
                flattened_corpus_embs.append(chunk)
                doc_to_chunk[f"{doc_id}~{i}"] = doc_id
                chunk_id_list.append(f"{doc_id}~{i}")
        flattened_corpus_embs = np.vstack(flattened_corpus_embs)
        flattened_corpus_embs = self._normalize(flattened_corpus_embs)
        return chunk_id_list, doc_to_chunk, flattened_corpus_embs

    @staticmethod
    def get_doc_results(results):
        doc_results = dict()
        for q, result_chunks in results.items():
            docs = dict()
            for c_id, score in result_chunks.items():
                d_id = '~'.join(c_id.split('~')[:-1])
                if (d_id not in docs) or (score > docs[d_id]):
                    docs[d_id] = float(score)
            doc_results[q] = docs
        return doc_results

    def _calculate_k_values(self, max_chunks):
        k_values = [1, 3, 5, 10, 20]
        n = 2
        while 10**n < 100 * max_chunks:
            k_values.append(10**n)
            n += 1
        return k_values

    def _apply_chunking(self, corpus, tokenizer):
        chunked_corpus = dict()
        for k, v in corpus.items():
            text = f"{v['title']} {v['text']}" if 'title' in v else v['text']
            current_doc = []
            chunk_annotations = self.chunker.chunk(
                text,
                tokenizer,
                chunking_strategy=self.chunking_strategy,
                **self.chunking_args,
            )
            tokens = tokenizer.encode_plus(text, add_special_tokens=False)
            for start_token_idx, end_token_idx in chunk_annotations:
                text_chunk = tokenizer.decode(
                    tokens.encodings[0].ids[start_token_idx:end_token_idx]
                )
                current_doc.append({'text': text_chunk})
            chunked_corpus[k] = current_doc
        return chunked_corpus

    def _calculate_annotations(self, model, corpus_texts):
        if self.model_has_instructions:
            instr = model.get_instructions()[1]
            instr_tokens = self.tokenizer(instr, add_special_tokens=False)
            n_instruction_tokens = len(instr_tokens[0])
        else:
            n_instruction_tokens = 0
        chunk_annotations = [
            self._extend_special_tokens(
                self.chunker.chunk(
                    text,
                    self.tokenizer,
                    chunking_strategy=self.chunking_strategy,
                    **self.chunking_args,
                ),
                n_instruction_tokens=n_instruction_tokens,
            )
            for text in corpus_texts
        ]
        return chunk_annotations

    @staticmethod
    def _flatten_chunks(chunked_corpus):
        flattened_corpus = dict()
        for k, li in chunked_corpus.items():
            for i, c in enumerate(li):
                flattened_corpus[f'{k}~{i}'] = c

        return flattened_corpus

    @staticmethod
    def _normalize(x):
        return x / np.linalg.norm(x, axis=1)[:, None]

    @staticmethod
    def _batch_inputs(li, batch_size):
        for i in range(0, len(li), batch_size):
            yield li[i : i + batch_size]

    @staticmethod
    def _extend_special_tokens(
        annotations, n_instruction_tokens=0, include_prefix=True, include_sep=True
    ):
        """Extends the spans because of additional special tokens, e.g. the CLS token
        which are not considered by the chunker.
        """
        new_annotations = []
        for i in range(len(annotations)):
            add_left_offset = 1 if (not include_prefix) or int(i > 0) else 0
            left_offset = 1 + n_instruction_tokens
            left = (
                annotations[i][0] + add_left_offset * left_offset
            )  # move everything by one for [CLS]

            add_sep = 1 if include_sep and ((i + 1) == len(annotations)) else 0
            right_offset = left_offset + add_sep
            right = (
                annotations[i][1] + right_offset
            )  # move everything by one for [CLS] and the last one for [SEP]

            new_annotations.append((left, right))
        return new_annotations

    @staticmethod
    def _prune(queries, corpus, relevant_docs, prune_size):
        new_queries = {'test': {}}
        new_corpus = {'test': {}}
        new_relevant_docs = {'test': {}}
        for i, key in enumerate(relevant_docs['test']):
            if i >= prune_size:
                break
            new_relevant_docs['test'][key] = relevant_docs['test'][key]
            for x in relevant_docs['test'][key]:
                new_corpus['test'][x] = corpus['test'][x]
            new_queries['test'][key] = queries['test'][key]
        return new_queries, new_corpus, new_relevant_docs

    def _calculate_metrics_from_split(*args, **kwargs):
        pass

    def _evaluate_subset(*args, **kwargs):
        pass
