import re
import pdb
from gensim import utils

import torch.multiprocessing as mp

from allennlp.data.iterators import BucketIterator
from allennlp.data.vocabulary import Vocabulary
from allennlp.modules.text_field_embedders import BasicTextFieldEmbedder
from allennlp.data.tokenizers.word_splitter import JustSpacesWordSplitter
from allennlp.data.token_indexers.elmo_indexer import ELMoTokenCharactersIndexer

from dataloaders.ir_triple_transformers_neutralityscores_loader import *
from dataloaders.ir_tuple_transformers_neutralityscores_loader import *
from typing import Dict, Tuple, List

from transformers import BertTokenizer, BartTokenizer

from fairness_measurement.document_neutrality import DocumentNeutrality

#
# Multiprocess input pipeline
# -------------------------------
#
# single epoch batch generators with multiple subprocesses, each subprocess works on its own file until the file is parsed completely
#
# - the processes have as little communication as possible (because it is prohibitly expensive in python)
# - the finished batches go into shared memory and then the queue to be picked up by the train/validaton loops
#

mp.get_logger().setLevel(logging.WARNING)  # ignore useless process start console logs
mp.set_sharing_strategy("file_system") # VERY MUCH needed for linux !! makes everything MUCH faster -> from 10 to 30+ batches/s

#
# process & queue starter, returns a queue which gets the batches put into ready to go into the model.forward pass
#
def get_multiprocess_batch_queue(name_prefix: str, target_function, files, conf, _logger, queue_size=100) -> Tuple[mp.Queue, List[mp.Process], mp.Event]:
    ctx = mp.get_context('spawn') # also set so that windows & linux behave the same 
    _queue = ctx.Queue(queue_size)
    _processes = []
    _finish_notification = ctx.Event()

    if len(files) == 0:
        _logger.error("No files for multiprocess loading specified, for: " + name_prefix)
        exit(1)
    else:
        _logger.info("Starting "+str(len(files))+" data loader processes, for:" + name_prefix)

    for proc_number, file in enumerate(files):
        process = ctx.Process(name=name_prefix + "-" + str(proc_number),
                             target=target_function,
                             args=(proc_number, conf, _queue, _finish_notification, file))
        process.start()
        _processes.append(process)
    return _queue, _processes, _finish_notification


#
# training instance generator
#   - filling the _queue with ready to run training batches
#   - everything is thread local
#
def multiprocess_training_loader(process_number: int, _config, _queue: mp.Queue, _wait_for_exit: mp.Event, 
                                 _local_file):

    _transformers_tokenizer = BertTokenizer.from_pretrained(_config["transformers_tokenizer_model_id"])
    _doc_neutrality = DocumentNeutrality(representative_words_path=_config["neutrality_representative_words_path"],
                                         threshold=_config["neutrality_threshold"],
                                         groups_portion={'f':0.5, 'm':0.5})

    _triple_loader  = IrTripleTransformersNeutralityScoresDatasetReader(lazy=True,
                                                                        transformers_tokenizer = _transformers_tokenizer,
                                                                        add_special_tokens = False,
                                                                        max_doc_length = _config["max_doc_length"],
                                                                        max_query_length = _config["max_query_length"],
                                                                        doc_neutrality=_doc_neutrality)
    _iterator = BucketIterator(batch_size=int(_config["batch_size_train"]),
                               sorting_keys=[("doc_pos_tokens", "dimension_0"), ("doc_neg_tokens", "dimension_0")])
    
    for training_batch in _iterator(_triple_loader.read(_local_file), num_epochs=1):
        _queue.put(training_batch)  # this moves the tensors in to shared memory
    _queue.put(None) # end of queue

    _queue.close()  # indicate this local thread is done
    _wait_for_exit.wait()  # keep this process alive until all the shared memory is used and not needed anymore

#
# validation instance generator
#   - filling the _queue with ready to run validation batches
#   - everything is defined thread local
#
def multiprocess_validation_loader(process_number: int, _config, _queue: mp.Queue, _wait_for_exit: mp.Event, 
                                   _local_file):

    _transformers_tokenizer = BertTokenizer.from_pretrained(_config["transformers_tokenizer_model_id"])
    _doc_neutrality = DocumentNeutrality(representative_words_path=_config["neutrality_representative_words_path"],
                                         threshold=_config["neutrality_threshold"],
                                         groups_portion={'f':0.5, 'm':0.5})

    _tuple_loader  = IrTupleTransformersNeutralityScoresDatasetReader(lazy=True,
                                                                      transformers_tokenizer=_transformers_tokenizer,
                                                                      add_special_tokens=False,
                                                                      max_doc_length=_config["max_doc_length"],
                                                                      max_query_length=_config["max_query_length"],
                                                                      doc_neutrality=_doc_neutrality)
    _iterator = BucketIterator(batch_size=int(_config["batch_size_train"]),
                               sorting_keys=[("doc_tokens", "dimension_0"), ("query_tokens", "dimension_0")])
    
    for _batch in _iterator(_tuple_loader.read(_local_file), num_epochs=1):
        if _batch is None:
            print ('a batch is null!!!')
        _queue.put(_batch)  # this moves the tensors in to shared memory
    _queue.put(None) # end of queue

    _queue.close()  # indicate this local thread is done
    _wait_for_exit.wait()  # keep this process alive until all the shared memory is used and not needed anymore



