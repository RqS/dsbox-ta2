import argparse
import getpass
import json
import os
import typing
import platform

import contextlib
import logging
import sys
import tempfile
import multiprocessing.managers

from pandas import DataFrame
from numpy import vectorize
from collections import defaultdict
from sklearn.model_selection import KFold, StratifiedKFold  # type: ignore
from d3m.metadata.problem import PerformanceMetric
from d3m.metadata.hyperparams import Hyperparams

import networkx  # type: ignore
from d3m.container.dataset import D3MDatasetLoader, Dataset
from d3m.metadata import base as metadata_base
from d3m.metadata.base import Metadata
from d3m.metadata.pipeline import Pipeline, PrimitiveStep, Resolver
from d3m.primitive_interfaces import base
from multiprocessing import current_process
import common_primitives.utils as utils
import d3m.metadata.base as mbase


_logger = logging.getLogger(__name__)

MAX_DUMP_SIZE = 50  # 1000


class Runtime:
    """
    Class to run the build and run a Pipeline.

    Attributes
    ----------
    pipeline_description : Pipeline
        A pipeline description to be executed.
    primitives_arguments: Dict[int, Dict[str, Dict]
        List of indexes reference to the arguments for each step.
    execution_order
        List of indexes that contains the execution order.
    pipeline
        List of different models generated by the primitives.
    outputs
        List of indexes reference how to build the the output.

    Parameters
    ----------
    pipeline_description : Pipeline
        A pipeline description to be executed.
    """

    def __init__(self, pipeline_description: Pipeline, fitted_pipeline_id: str, log_dir) -> None:
        self.pipeline_description = pipeline_description
        self.fitted_pipeline_id = fitted_pipeline_id

        n_steps = len(self.pipeline_description.steps)

        self.primitives_arguments: typing.Dict[int, typing.Dict[str, typing.Dict]] = {}
        for i in range(0, n_steps):
            self.primitives_arguments[i] = {}

        self.execution_order: typing.List[int] = []

        self.pipeline: typing.List[typing.Optional[base.PrimitiveBase]] = [None] * n_steps
        self.outputs: typing.List[typing.Tuple[str, int]] = []
        self.log_dir = log_dir

        # Getting the outputs
        for output in self.pipeline_description.outputs:
            origin = output['data'].split('.')[0]
            source = output['data'].split('.')[1]
            self.outputs.append((origin, int(source)))

        # Constructing DAG to determine the execution order
        execution_graph = networkx.DiGraph()
        for i in range(0, n_steps):
            primitive_step: PrimitiveStep = typing.cast(PrimitiveStep, self.pipeline_description.steps[i])
            for argument, data in primitive_step.arguments.items():
                argument_edge = data['data']
                origin = argument_edge.split('.')[0]
                source = argument_edge.split('.')[1]

                self.primitives_arguments[i][argument] = {'origin': origin, 'source': int(source)}

                if origin == 'steps':
                    execution_graph.add_edge(str(source), str(i))
                else:
                    execution_graph.add_edge(origin, str(i))

        execution_order = list(networkx.topological_sort(execution_graph))

        # Removing non-step inputs from the order
        execution_order = list(filter(lambda x: x.isdigit(), execution_order))
        self.execution_order = [int(x) for x in execution_order]

        # Creating set of steps to be call in produce
        self.produce_order: typing.Set[int] = set()
        for output in self.pipeline_description.outputs:
            origin = output['data'].split('.')[0]
            source = output['data'].split('.')[1]
            if origin != 'steps':
                continue
            else:
                current_step = int(source)
                self.produce_order.add(current_step)
                for i in range(0, len(execution_order)):
                    step_origin = self.primitives_arguments[current_step]['inputs']['origin']
                    step_source = self.primitives_arguments[current_step]['inputs']['source']
                    if step_origin != 'steps':
                        break
                    else:
                        self.produce_order.add(step_source)
                        current_step = step_source
        # kyao!!!!
        self.produce_order = set(self.execution_order)
        self.fit_outputs: typing.List = []
        self.produce_outputs: typing.List = []
        self.metric_descriptions: typing.List = []
        self.cross_validation_result: typing.List = []

    def set_metric_descriptions(self, metric_descriptions):
        self.metric_descriptions = metric_descriptions

    def fit(self, **arguments: typing.Any) -> None:
        """
        Train all steps in the pipeline.

        Paramters
        ---------
        arguments
            Arguments required to train the Pipeline
        """
        if 'cache' in arguments:
            cache = arguments['cache']
        else:
            cache = {}

        primitives_outputs: typing.List[typing.Optional[base.CallResult]] = [None] * len(self.execution_order)

        hash_prefix = ""

        for i in range(0, len(self.execution_order)):
            primitive_arguments: typing.Dict[str, typing.Any] = {}
            n_step = self.execution_order[i]
            for argument, value in self.primitives_arguments[n_step].items():
                if value['origin'] == 'steps':
                    primitive_arguments[argument] = primitives_outputs[value['source']]
                else:
                    primitive_arguments[argument] = arguments[argument][value['source']]

            if isinstance(self.pipeline_description.steps[n_step], PrimitiveStep):
                # first we need to compute the key to query in cache. For the key we use a hashed combination of the primitive name,
                # its hyperparameters and its input dataset hash.
                hyperparam_hash = hash(str(self.pipeline_description.steps[n_step].hyperparams.items()))

                dataset_id = ""
                dataset_digest = ""
                try:
                    dataset_id = str(primitive_arguments['inputs'].metadata.query(())['id'])
                    dataset_digest = str(primitive_arguments['inputs'].metadata.query(())['digest'])
                except:
                    pass
                dataset_hash = hash(str(primitive_arguments) + dataset_id + dataset_digest)

                prim_name = str(self.pipeline_description.steps[n_step].primitive)
                prim_hash = hash(str([hyperparam_hash, dataset_hash, hash_prefix]))

                hash_prefix = prim_hash

                _logger.info(
                    "Primitive Fit. 'id': '%(primitive_id)s', '(name, hash)': ('%(name)s', '%(hash)s'), 'worker_id': '%(worker_id)s'.",
                    {
                        'primitive_id': self.pipeline_description.steps[n_step].primitive_description['id'],
                        'name': prim_name,
                        'hash': prim_hash,
                        'worker_id': current_process()
                    },
                )
                _logger.debug('name: %s hyperparams: %s', prim_name, str(self.pipeline_description.steps[n_step].hyperparams))

                if (prim_name, prim_hash) in cache:
                    # primitives_outputs[n_step],model =
                    # self._primitive_step_fit(n_step,
                    # self.pipeline_description.steps[n_step],
                    # primitive_arguments)
                    primitives_outputs[n_step], model = cache[
                        (prim_name, prim_hash)]
                    self.pipeline[n_step] = model
                    print("[INFO] Hit@cache:", (prim_name, prim_hash))
                    _logger.debug("Hit@cache: (%s, %s)", prim_name, prim_hash)

                    # assert type()

                else:
                    print("[INFO] Push@cache:", (prim_name, prim_hash))
                    _logger.debug("Push@cache: (%s, %s)", prim_name, prim_hash)
                    primitive_step: PrimitiveStep = \
                        typing.cast(PrimitiveStep,
                                    self.pipeline_description.steps[n_step]
                                    )

                    primitives_outputs[n_step], model = \
                        self._primitive_step_fit(n_step,
                                                 primitive_step,
                                                 primitive_arguments
                                                 )

                    # add the entry to cache:
                    try:
                        # copying back sklearn_wrap.SKGenericUnivariateSelect fails
                        cache[(prim_name, prim_hash)] = (primitives_outputs[n_step].copy(), model)
                    except:
                        _logger.info('Push Cache failed: (%s, %s)', prim_name, prim_hash)

                    if _logger.getEffectiveLevel() <= 10:

                        # _logger.debug('cache keys')
                        # for key in sorted(cache.keys()):
                        #     _logger.debug('   {}'.format(key))

                        debug_file = os.path.join(
                            self.log_dir, 'dfs',
                            'fit_{}_{}_{:02}_{}'.format(self.pipeline_description.id, self.fitted_pipeline_id, n_step, primitive_step.primitive))
                        _logger.debug(
                            "'id': '%(pipeline_id)s', 'fitted': '%(fitted_pipeline_id)s', 'name': '%(name)s', 'worker_id': '%(worker_id)s'. Output is written to: '%(path)s'.",
                            {
                                'pipeline_id': self.pipeline_description.id,
                                'fitted_pipeline_id': self.fitted_pipeline_id,
                                'name': primitive_step.primitive,
                                'worker_id': current_process(),
                                'path': debug_file
                            },
                        )
                        if primitives_outputs[n_step] is None:
                            with open(debug_file) as f:
                                f.write("None")
                        else:
                            if isinstance(primitives_outputs[n_step], DataFrame):
                                try:
                                    primitives_outputs[n_step][:MAX_DUMP_SIZE].to_csv(debug_file)
                                except:
                                    pass

        # kyao!!!!
        self.fit_outputs = primitives_outputs

    def _primitive_step_fit(self, n_step: int, step: PrimitiveStep, primitive_arguments: typing.Dict[str, typing.Any]) -> base.CallResult:
        """
        Execute a step and train it with primitive arguments.

        Paramters
        ---------
        n_step: int
            An integer of the actual step.
        step: PrimitiveStep
            A primitive step.
        primitive_arguments
            Arguments for set_training_data, fit, produce of the primitive for this step.

        """
        primitive: typing.Type[base.PrimitiveBase] = step.primitive
        primitive_hyperparams = primitive.metadata.query()['primitive_code']['class_type_arguments']['Hyperparams']
        custom_hyperparams = dict()

        if bool(step.hyperparams):
            for hyperparam, value in step.hyperparams.items():
                if isinstance(value, dict):
                    custom_hyperparams[hyperparam] = value['data']
                else:
                    custom_hyperparams[hyperparam] = value

        training_arguments_primitive = self._primitive_arguments(primitive, 'set_training_data')
        training_arguments: typing.Dict[str, typing.Any] = {}
        produce_params_primitive = self._primitive_arguments(primitive, 'produce')
        produce_params: typing.Dict[str, typing.Any] = {}

        for param, value in primitive_arguments.items():
            if param in produce_params_primitive:
                produce_params[param] = value
            if param in training_arguments_primitive:
                training_arguments[param] = value
        try:
            model = primitive(hyperparams=primitive_hyperparams(
                primitive_hyperparams.defaults(), **custom_hyperparams))
        except:
            print("******************\n[ERROR]Hyperparameters unsuccesfully set - using defaults")
            model = primitive(hyperparams=primitive_hyperparams(primitive_hyperparams.defaults()))

        # kyao!!!!
        # now only run when "cross_validation" was found
        # TODO: add one more "if" to restrict runtime to run cross validation only for tuning steps
        # if step_number == pass_in_number
        if 'runtime' in step.primitive_description and "cross_validation" in step.primitive_description['runtime']:
            self.cross_validation_result = self._cross_validation(
                primitive, training_arguments, produce_params, primitive_hyperparams, custom_hyperparams,
                step.primitive_description['runtime'])

        model.set_training_data(**training_arguments)
        model.fit()
        self.pipeline[n_step] = model

        if str(primitive) == 'd3m.primitives.dsbox.Encoder':
            total_columns = self._total_encoder_columns(model, produce_params['inputs'])
            if total_columns > 500:
                raise Exception('Total column limit exceeded after encoding: {}'.format(total_columns))

        if str(primitive) == 'd3m.primitives.dsbox.CleaningFeaturizer':
            model = self._work_around_for_cleaning_featurizer(model, training_arguments['inputs'])

        if str(primitive) == 'd3m.primitives.dsbox.Profiler':
            this_step_result = model.produce(**produce_params).value
            produce_result = self._work_around_for_profiler(this_step_result)
        else:
            produce_result = model.produce(**produce_params).value

        return produce_result, model

    def _cross_validation(self, primitive: typing.Type[base.PrimitiveBase],
                          training_arguments: typing.Dict,
                          produce_params: typing.Dict,
                          primitive_hyperparams: Hyperparams,
                          custom_hyperparams: typing.Dict,
                          runtime_instr: typing.Dict,
                          seed: int = 4767) -> typing.List:

        _logger.debug('cross-val primitive: %s' % str(primitive))

        results: typing.List[str, typing.Dict] = []

        validation_metrics: typing.Dict[str, typing.List[float]] = defaultdict(list)
        targets: typing.Dict[str, typing.List[list]] = defaultdict(list)

        X = training_arguments['inputs']
        y = training_arguments['outputs']

        cv = runtime_instr.get('cross_validation', 10)
        use_stratified = runtime_instr.get('stratified', False)

        # Redirect stderr to an error file
        #  Directly assigning stderr to tempfile.TemporaryFile cause printing str to fail
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, str(primitive)), 'w') as errorfile:
                with contextlib.redirect_stderr(errorfile):

                    if use_stratified:
                        kf = StratifiedKFold(n_splits=cv, shuffle=True, random_state=seed)
                    else:
                        kf = KFold(n_splits=cv, shuffle=True, random_state=seed)

                    num = 0.0
                    for k, (train, test) in enumerate(kf.split(X, y)):

                        try:
                            model = primitive(hyperparams=primitive_hyperparams(
                                primitive_hyperparams.defaults(), **custom_hyperparams))
                        except:
                            print("******************\n[ERROR]Hyperparameters unsuccesfully set - using defaults")
                            model = primitive(hyperparams=primitive_hyperparams(primitive_hyperparams.defaults()))

                        if model is None:
                            return results

                        trainX = X.take(train, axis=0)
                        trainY = y.take(train, axis=0).values.ravel()
                        testX = X.take(test, axis=0)
                        testY = y.take(test, axis=0).values.ravel()

                        validation_train = dict(training_arguments)
                        validation_train['inputs'] = trainX
                        validation_train['outputs'] = trainY

                        validation_test = dict(produce_params)
                        validation_test['inputs'] = testX

                        try:
                            model.set_training_data(**validation_train)
                            model.fit()
                            ypred = model.produce(**validation_test).value

                            num = num + 1.0

                            targets['ground_truth'].append(testY)
                            targets['prediction'].append(ypred)
                            for metric_description in self.metric_descriptions:
                                metricDesc = PerformanceMetric.parse(metric_description['metric'])
                                metric: typing.Callable = metricDesc.get_function()
                                params: typing.Dict = metric_description['params']
                                validation_metrics[metric_description['metric']].append(metric(testY, ypred, **params))

                        except Exception as e:
                            sys.stderr.write("ERROR: cross_validation {}: {}\n".format(primitive, e))
                            # traceback.print_exc(e)

        if num == 0:
            return results

        average_metrics: typing.Dict[str, dict] = {}
        for name, values in validation_metrics.items():
            average_metrics[name] = sum(values) / len(values)

        for metric_description in self.metric_descriptions:
            result_by_metric = {}
            result_by_metric['metric'] = metric_description['metric']
            result_by_metric['value'] = average_metrics[metric_description['metric']]
            result_by_metric['values'] = validation_metrics[metric_description['metric']]
            result_by_metric['targets'] = targets[metric_description['metric']]
            results.append(result_by_metric)

        for result in results:
            _logger.debug('cross-validation metric: %s=%.4f', result['metric'], result['value'])
            _logger.debug('cross-validation details: %s %s',
                          result['metric'], str(['%.4f' % x for x in result['values']]))

        return results

    def _primitive_arguments(self, primitive: typing.Type[base.PrimitiveBase], method: str) -> set:
        """
        Get the arguments of a primitive given a function.

        Paramters
        ---------
        primitive
            A primitive.
        method
            A method of the primitive.
        """
        return set(primitive.metadata.query()['primitive_code']['instance_methods'][method]['arguments'])

    def produce(self, **arguments: typing.Any) -> typing.List:
        """
        Train all steps in the pipeline.

        Paramters
        ---------
        arguments
            Arguments required to execute the Pipeline
        """
        steps_outputs = [None] * len(self.execution_order)

        for i in range(0, len(self.execution_order)):
            n_step = self.execution_order[i]
            primitive_step: PrimitiveStep = typing.cast(PrimitiveStep, self.pipeline_description.steps[n_step])
            produce_arguments_primitive = self._primitive_arguments(primitive_step.primitive, 'produce')
            produce_arguments: typing.Dict[str, typing.Any] = {}

            _logger.info(
                "Primitive Produce. 'id': '%(primitive_id)s', 'name': '%(name)s', 'worker_id': '%(worker_id)s'.",
                {
                    'primitive_id': primitive_step.primitive_description['id'],
                    'name': primitive_step.primitive,
                    'worker_id': current_process()
                },
            )

            for argument, value in self.primitives_arguments[n_step].items():
                if argument in produce_arguments_primitive:
                    if value['origin'] == 'steps':
                        produce_arguments[argument] = steps_outputs[value['source']]
                    else:
                        produce_arguments[argument] = arguments[argument][value['source']]
                    if produce_arguments[argument] is None:
                        continue
            if isinstance(self.pipeline_description.steps[n_step], PrimitiveStep):
                if n_step in self.produce_order:
                    if str(primitive_step.primitive) == 'd3m.primitives.dsbox.Profiler':
                        this_step_result = self.pipeline[n_step].produce(**produce_arguments).value
                        steps_outputs[n_step] = self._work_around_for_profiler(this_step_result)
                    
                    elif str(primitive_step.primitive) == 'd3m.primitives.dsbox.TimeseriesToList':
                        this_step_result = self.pipeline[n_step].produce(**produce_arguments).value
                        steps_outputs[n_step] = self._work_around_for_timeseries(this_step_result)

                    else:
                        steps_outputs[n_step] = self.pipeline[n_step].produce(**produce_arguments).value
                else:
                    steps_outputs[n_step] = None

            if _logger.getEffectiveLevel() <= 10:
                debug_file = os.path.join(self.log_dir, 'dfs',
                                          'pro_{}_{}_{:02}_{}'.format(self.pipeline_description.id, self.fitted_pipeline_id, n_step, primitive_step.primitive))
                _logger.debug(
                    "'id': '%(pipeline_id)s', 'fitted': '%(fitted_pipeline_id)s', 'name': '%(name)s', 'worker_id': '%(worker_id)s'. Output is written to: '%(path)s'.",
                    {
                        'pipeline_id': self.pipeline_description.id,
                        'fitted_pipeline_id': self.fitted_pipeline_id,
                        'name': primitive_step.primitive,
                        'worker_id': current_process(),
                        'path': debug_file
                    },
                )
                if steps_outputs[n_step] is None:
                    with open(debug_file) as f:
                        f.write("None")
                else:
                    if isinstance(steps_outputs[n_step], DataFrame):
                        try:
                            steps_outputs[n_step][:MAX_DUMP_SIZE].to_csv(debug_file)
                        except:
                            pass

        # kyao!!!!
        self.produce_outputs = steps_outputs

        # Create output
        pipeline_output: typing.List = []
        for output in self.outputs:
            if output[0] == 'steps':
                pipeline_output.append(steps_outputs[output[1]])
            else:
                pipeline_output.append(arguments[output[0][output[1]]])
        return pipeline_output

    @staticmethod
    def _total_encoder_columns(encoder_primitive, df):
        count = df.shape[1] - len(encoder_primitive._empty_columns) - len(encoder_primitive._cat_columns)
        for values in encoder_primitive._mapping.values():
            count += len(values) + 1
        _logger.info('Encoder: column count before={} after={}'.format(df.shape[1], count))
        return count

    @staticmethod
    def _work_around_for_timeseries(df):
        # drop the object columns
        for i in range(len(df[1])):
            for each_column in df[1][i].columns:
                if str(df[1][i][each_column].dtypes) == "object":
                    df[1][i] = df[1][i].drop(columns = [each_column])
        return df

    @staticmethod
    def _work_around_for_profiler(df):
        float_cols = utils.list_columns_with_semantic_types(df.metadata, ['http://schema.org/Float'])

        # !!! Do not delete these codes, those code is used to keep the fileName column
        filename_cols = list(set(utils.list_columns_with_semantic_types(df.metadata, [
            'https://metadata.datadrivendiscovery.org/types/Time'])).intersection(
            utils.list_columns_with_semantic_types(df.metadata,
                                                   ["https://metadata.datadrivendiscovery.org/types/FileName"])))

        for col in filename_cols:
            old_metadata = dict(df.metadata.query((mbase.ALL_ELEMENTS, col)))
            old_metadata['semantic_types'] = tuple(x for x in old_metadata['semantic_types'] if
                                                   x != 'https://metadata.datadrivendiscovery.org/types/Time')
            df.metadata = df.metadata.update((mbase.ALL_ELEMENTS, col), old_metadata)
        for col in float_cols:
            old_metadata = dict(df.metadata.query((mbase.ALL_ELEMENTS, col)))
            if 'https://metadata.datadrivendiscovery.org/types/Attribute' not in old_metadata['semantic_types']:
                old_metadata['semantic_types'] += ('https://metadata.datadrivendiscovery.org/types/Attribute',)
                df.metadata = df.metadata.update((mbase.ALL_ELEMENTS, col), old_metadata)

        return df

    @staticmethod
    def _work_around_for_cleaning_featurizer(model, inputs):
        vector_cols = list(set(utils.list_columns_with_semantic_types(inputs.metadata, [
            'https://metadata.datadrivendiscovery.org/types/FloatVector'])).intersection(
            utils.list_columns_with_semantic_types(inputs.metadata,
                                                   ["https://metadata.datadrivendiscovery.org/types/Location"])))
        for col in vector_cols:
            try:
                n = 10
                split_to = sum(inputs.iloc[:n, col].apply(str).apply(vectorize(lambda x: len(x.split(','))))) // n
            except:
                split_to = 2

            try:
                if 'alpha_numeric_columns' not in model._mapping:
                    model._mapping['alpha_numeric_columns'] = {
                        "columns_to_perform": [col],
                        "split_to": [split_to]
                    }
                else:
                    if 'columns_to_perform' in model._mapping['alpha_numeric_columns']:
                        model._mapping['alpha_numeric_columns']['columns_to_perform'].append(col)
                        model._mapping['alpha_numeric_columns']['split_to'].append(split_to)
                    else:
                        model._mapping['alpha_numeric_columns'] = {
                            "columns_to_perform": [col],
                            "split_to": [split_to]
                        }
            except:
                pass
        return model


def load_problem_doc(problem_doc_path: str) -> Metadata:
    """
    Load problem_doc from problem_doc_path

    Paramters
    ---------
    problem_doc_path
        Path where the problemDoc.json is located
    """

    with open(problem_doc_path) as file:
        problem_doc = json.load(file)
    return Metadata(problem_doc)


def add_target_columns_metadata(dataset: 'Dataset', problem_doc_metadata: 'Metadata') -> Dataset:
    """
    Add metadata to the dataset from problem_doc_metadata

    Paramters
    ---------
    dataset
        Dataset
    problem_doc_metadata:
        Metadata about the problemDoc
    """

    for data in problem_doc_metadata.query(())['inputs']['data']:
        targets = data['targets']
        for target in targets:
            semantic_types = list(dataset.metadata.query(
                (target['resID'], metadata_base.ALL_ELEMENTS, target['colIndex'])).get('semantic_types', []))

            if 'https://metadata.datadrivendiscovery.org/types/Target' not in semantic_types:
                semantic_types.append('https://metadata.datadrivendiscovery.org/types/Target')
                dataset.metadata = dataset.metadata.update(
                    (target['resID'], metadata_base.ALL_ELEMENTS, target['colIndex']), {'semantic_types': semantic_types})

            if 'https://metadata.datadrivendiscovery.org/types/TrueTarget' not in semantic_types:
                semantic_types.append('https://metadata.datadrivendiscovery.org/types/TrueTarget')
                dataset.metadata = dataset.metadata.update(
                    (target['resID'], metadata_base.ALL_ELEMENTS, target['colIndex']), {'semantic_types': semantic_types})

    return dataset


def generate_pipeline(pipeline_path: str, dataset_path: str, problem_doc_path: str, resolver: Resolver = None) -> Runtime:
    """
    Simplified interface that fit a pipeline with a dataset

    Paramters
    ---------
    pipeline_path
        Path to the pipeline description
    dataset_path:
        Path to the datasetDoc.json
    problem_doc_path:
        Path to the problemDoc.json
    resolver : Resolver
        Resolver to use.
    """

    # Pipeline description
    pipeline_description = None
    if '.json' in pipeline_path:
        with open(pipeline_path) as pipeline_file:
            pipeline_description = Pipeline.from_json(string_or_file=pipeline_file, resolver=resolver)
    else:
        with open(pipeline_path) as pipeline_file:
            pipeline_description = Pipeline.from_yaml(string_or_file=pipeline_file, resolver=resolver)

    # Problem Doc
    problem_doc = load_problem_doc(problem_doc_path)

    # Dataset
    if 'file:' not in dataset_path:
        dataset_path = 'file://{dataset_path}'.format(dataset_path=os.path.abspath(dataset_path))

    dataset = D3MDatasetLoader().load(dataset_uri=dataset_path)
    # Adding Metadata to Dataset
    dataset = add_target_columns_metadata(dataset, problem_doc)

    # Pipeline
    pipeline_runtime = Runtime(pipeline_description)
    # Fitting Pipeline
    pipeline_runtime.fit(inputs=[dataset])
    return pipeline_runtime


def test_pipeline(pipeline_runtime: Runtime, dataset_path: str) -> typing.List:
    """
    Simplified interface test a pipeline with a dataset

    Paramters
    ---------
    pipeline_runtime
        Runtime object
    dataset_path:
        Path to the datasetDoc.json
    """

    # Dataset
    if 'file:' not in dataset_path:
        dataset_path = 'file://{dataset_path}'.format(dataset_path=os.path.abspath(dataset_path))
    dataset = D3MDatasetLoader().load(dataset_uri=dataset_path)

    return pipeline_runtime.produce(inputs=[dataset])


def load_args() -> typing.Tuple[str, str]:
    parser = argparse.ArgumentParser(description="Run pipelines.")

    parser.add_argument(
        'pipeline', action='store', metavar='PIPELINE',
        help="path to a pipeline file (.json or .yml)",
    )

    parser.add_argument(
        'dataset', action='store', metavar='DATASET',
        help="path to the primary datasetDoc.json for the dataset you want to use.",
    )

    arguments = parser.parse_args()

    return os.path.abspath(arguments.pipeline), os.path.abspath(arguments.dataset)


def main() -> None:
    pipeline_path, dataset_path = load_args()

    base_dataset_dir = os.path.abspath(os.path.join(dataset_path, os.pardir, os.pardir))
    train_dataset_doc = os.path.join(base_dataset_dir, 'TRAIN', 'dataset_TRAIN', 'datasetDoc.json')
    train_problem_doc = os.path.join(base_dataset_dir, 'TRAIN', 'problem_TRAIN', 'problemDoc.json')
    test_dataset_doc = os.path.join(base_dataset_dir, 'TEST', 'dataset_TEST', 'datasetDoc.json')

    pipeline_runtime = generate_pipeline(
        pipeline_path=pipeline_path,
        dataset_path=train_dataset_doc,
        problem_doc_path=train_problem_doc)

    results = test_pipeline(pipeline_runtime, test_dataset_doc)
    print(results)


if __name__ == '__main__':
    main()
