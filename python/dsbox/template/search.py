import bisect
import operator
import os
import random
import time
import traceback
import typing
import logging
import time
from warnings import warn

from multiprocessing import Pool, current_process, Manager
from itertools import zip_longest
from pprint import pprint

from d3m.exceptions import NotSupportedError
from d3m.container.dataset import Dataset
from d3m.metadata.base import Metadata
from d3m.metadata.base import ALL_ELEMENTS
from d3m.metadata.problem import PerformanceMetric
from d3m.primitive_interfaces.base import PrimitiveBaseMeta

from dsbox.pipeline.fitted_pipeline import FittedPipeline
from dsbox.schema.problem import optimization_type
from dsbox.schema.problem import OptimizationType

from .template import DSBoxTemplate
from .template import HyperparamDirective

from .configuration_space import DimensionName
from .configuration_space import ConfigurationPoint
from .configuration_space import ConfigurationSpace
from .configuration_space import SimpleConfigurationSpace

from .pipeline_utilities import pipe2str


T = typing.TypeVar("T")
_logger = logging.getLogger(__name__)


def get_target_columns(dataset: 'Dataset', problem_doc_metadata: 'Metadata'):
    problem = problem_doc_metadata.query(())["inputs"]["data"]
    datameta = dataset.metadata
    target = problem[0]["targets"]
    resID = target[0]["resID"]
    colIndex = target[0]["colIndex"]
    datalength = datameta.query((resID, ALL_ELEMENTS,))["dimension"]['length']
    targetlist = []
    for v in range(datalength):
        types = datameta.query((resID, ALL_ELEMENTS, v))["semantic_types"]
        for t in types:
            if t == 'https://metadata.datadrivendiscovery.org/types/PrimaryKey':
                targetlist.append(v)
    targetlist.append(colIndex)
    targetcol = dataset[resID].iloc[:, targetlist]
    return targetcol


class DimensionalSearch(typing.Generic[T]):
    """
    Search configuration space on dimension at a time.

    Attributes
    ----------
    evaluate : Callable[[typing.Dict], float]
        Evaluate given point in configuration space
    configuration_space: ConfigurationSpace[T]
        Definition of the configuration space
    minimize: bool
        If True, minimize the value returned by `evaluate` function
    """

    def __init__(self, evaluate: typing.Callable[[ConfigurationPoint[T]], typing.Tuple[float, dict]],
                 configuration_space: ConfigurationSpace[T], minimize: bool) -> None:
        self.evaluate = evaluate
        self.configuration_space = configuration_space
        self.minimize = minimize
        self.dimension_ordering = configuration_space.get_dimension_search_ordering()

    def random_assignment(self) -> typing.Dict[DimensionName, T]:
        """
        Randomly assigns a value for each dimension
        """
        assignment: typing.Dict[DimensionName, T] = {}
        for dimension in self.dimension_ordering:
            assignment[dimension] = random.choice(
                self.configuration_space.get_values(dimension))
        return assignment

    def first_assignment(self) -> typing.Dict[DimensionName, T]:
        '''
        Assign the first value for each dimension
        '''
        assignment: typing.Dict[DimensionName, T] = {}
        for dimension in self.dimension_ordering:
            assignment[dimension] = self.configuration_space.get_values(dimension)[0]
        return assignment

    def get_dimension_length(self, kw: DimensionName) -> int:
        '''
        Return the length of the list a configuration point
        '''
        return len(self.configuration_space.get_values(kw))

    def generate_pipeline(self, configuration_space: ConfigurationSpace[T],
                          dimension: typing.List[DimensionName]):
        pass

    def _lookup_candidate(self, candidate: ConfigurationPoint[T], cand_cache: typing.Dict) -> \
            typing.Tuple[ConfigurationPoint[T], float]:

        key = hash(str(candidate))
        if key in cand_cache:
            line = cand_cache[key]
            print("[INFO] hit@Candidate: ({},{})".format(key, line["id"]))
            # print("[INFO] candidate cache Hit@{}:{}".format(key, line['candidate']))
            return line['candidate'], line['value']
        else:
            return (None, None)

    def _push_candidate(self, result: typing.Dict, candidate: ConfigurationPoint[T],
                        cand_cache: typing.Dict) -> None:

        key = hash(str(candidate))
        cand_id = result['fitted_pipeline'].id if result else None
        value = result['test_metrics'][0]['value'] if result else None
        print("[INFO] push@Candidate: ({},{})".format(key, cand_id))
        # add value to candidate cache

        if self._is_candidate_hit(candidate, cand_cache):
            assert value == cand_cache[key]['value'], \
                "New value for candidate:" + str(candidate)
            return

        cand_cache[key] = {
            "candidate": candidate,
            "id": cand_id,
            "value": value,
        }

    def _is_candidate_hit(self, candidate: ConfigurationPoint[T], cand_cache: typing.Dict) -> bool:
        return hash(str(candidate)) in cand_cache

    def search_one_iter(self, candidate_in: ConfigurationPoint[T] = None,

                        max_per_dimension: int=50,
                        cache_bundle: typing.Tuple[typing.Dict, typing.Dict]=(None, None)) -> \
            typing.Dict:
        """
        Performs one iteration of dimensional search. During dimesional
        search our algorithm iterates through all 8 steps of pipeline as
        indicated in our configuration space and greedily optimizes the
        pipeline one step at a time.

        Parameters
        ----------
        candidate_in: ConfigurationPoint[T]
            Current best candidate
        max_per_dimension: int
            Maximum number of values to search per dimension
        cache_bundle: tuple[Dict,Dict]
            the global cache object and candidate cache object for storing and reusing computation
            on intermediate results. if the cache is None, a local cache will be used in the
            dimensional search
        """

        # setup the output cache
        local_cache = False
        if cache_bundle[0] is None or cache_bundle[1] is None:
            print("[INFO] Using Local Cache")
            local_cache = True
            manager = Manager()
            cache = manager.dict()
            candidate_cache = manager.dict()
        else:
            cache, candidate_cache = cache_bundle

        # initialize the simulation counter
        sim_counter = 0
        start_time = time.clock()

        # we first need the baseline for searching the conf_space. For this
        # purpose we initially use first configuration and evaluate it on the
        #  dataset. In case that failed we repeat the sampling process one
        # more time to guarantee robustness on error reporting

        candidate, candidate_value = \
            self.setup_initial_candidate(candidate_in, cache, candidate_cache)

        sim_counter += 1
        # generate an executable pipeline with random steps from conf. space.
        # The actual searching process starts here.

        for dimension in self.dimension_ordering:
            # get all possible choices for the step, as specified in
            # configuration space
            choices: typing.List[T] = self.configuration_space\
                                          .get_values(dimension)

            # TODO this is just a hack
            if len(choices) == 1:  # if only have one candidate primitive for this step, skip?
                continue
            # print("[INFO] choices:", choices, ", in step:", dimension)
            assert 1 < len(choices), \
                f'Step {dimension} has not primitive choices!'

            # the weights are assigned by template designer
            weights = [self.configuration_space.get_weight(
                dimension, x) for x in choices]

            # generate the candidates choices list
            selected = random_choices_without_replacement(
                choices, weights, max_per_dimension)

            # No need to evaluate if value is already known
            if candidate_value is not None and candidate[dimension] in selected:
                selected.remove(candidate[dimension])

            # all the new possible pipelines are generated in new_candidates
            new_candidates: typing.List[ConfigurationPoint] = []
            for value in selected:
                # transfer the pipeline to dictionary type so that we can change detail steps
                new = dict(candidate)
                # replace the traget step
                new[dimension] = value
                # regenerate the pipeline
                candidate_ = self.configuration_space.get_point(new)

                if not self._is_candidate_hit(candidate_, candidate_cache):
                    new_candidates.append(candidate_)

            test_values = []
            cross_validation_values = []
            sucessful_candidates = []
            best_index = -1
            print('*' * 100)
            print("[INFO] Running Pool for step", dimension, ", fork_num:", len(new_candidates))
            sim_counter += len(new_candidates)
            try:
                with Pool(self.num_workers) as p:
                    results = p.map(
                        self.evaluate,
                        map(lambda c: (c, cache), new_candidates)
                    )

                # results = map(self.evaluate,new_candidates)
                for res, x in zip(results, new_candidates):
                    self._push_candidate(res, x, candidate_cache)
                    if not res:
                        print('[ERROR] candidate failed:')
                        pprint(x)
                        print("-" * 10)
                        continue

                    test_values.append(res['test_metrics'][0]['value'])
                    if res['cross_validation_metrics']:
                        cross_validation_values.append(res['cross_validation_metrics'][0]['value'])
                    res['fitted_pipeline'] = res['fitted_pipeline']
                    x.data.update(res)
                    sucessful_candidates.append(x)
            except:
                traceback.print_exc()

            # All candidates failed!
            if len(test_values) == 0:
                print("[INFO] No new Candidate worked in this step!")
                if not candidate:
                    print("[ERROR] The template did not return any valid pipelines!")
                    return (None, None)
                else:
                    continue

            # Find best candidate
            best_cv_index = 0  # initialize best_cv_index
            if self.minimize:
                best_index = test_values.index(min(test_values))
                if cross_validation_values:
                    best_cv_index = cross_validation_values.index(min(cross_validation_values))
            else:
                best_index = test_values.index(max(test_values))
                if cross_validation_values:
                    best_cv_index = cross_validation_values.index(max(cross_validation_values))
            print("[INFO] Best index:", best_index, "___", test_values[best_index])
            if cross_validation_values:
                if best_index == best_cv_index:
                    print("[INFO] Best CV index:", best_cv_index,
                          "___", cross_validation_values[best_cv_index])
                else:
                    print("[WARN] Best CV index:", best_cv_index,
                          "___", cross_validation_values[best_cv_index])
                    print("[WARN] CV detail values:",
                          ['{:.4f}'.format(x) for x in
                           results[best_cv_index]['cross_validation_metrics'][0]['values']])
            if candidate_value is None:
                candidate = sucessful_candidates[best_index]
                candidate_value = test_values[best_index]
            elif (self.minimize and test_values[best_index] < candidate_value) or \
                    (not self.minimize and test_values[best_index] > candidate_value):
                candidate = sucessful_candidates[best_index]
                candidate_value = test_values[best_index]
            # assert "fitted_pipe" in candidate.data, "parameters not added! loop"
        # END FOR

        # shutdown the cache manager
        if local_cache:
            manager.shutdown()

        # here we can get the details of pipelines from "candidate.data"
        assert "fitted_pipeline" in candidate.data, "parameters not added! last"
        # gather information for UCT

        reward = candidate_value

        # TODO : come up with a better method for this
        if self.minimize:
            reward = -reward

        UCT_report = {
            'reward': reward,
            'time': time.clock() - start_time,
            'sim_count': sim_counter,
            'candidate': candidate,
            'best_val': candidate_value
        }
        # return (candidate, candidate_value)
        return UCT_report

    def setup_initial_candidate(self,
                                candidate: ConfigurationPoint[T],
                                cache: typing.Dict,
                                candidate_cache: typing.Dict) -> \
            typing.Tuple[ConfigurationPoint[T], float]:
        """
        we first need the baseline for searching the conf_space. For this
        purpose we initially use first configuration and evaluate it on the
        dataset. In case that failed we repeat the sampling process one more
        time to guarantee robustness on error reporting

        Args:
            candidate: ConfigurationPoint[T]
                the previous best candidate. In case it is None, random candidate will be
                evaluated. if the random candidates fail on the dataset the process of choosing
                a random candidate will be repeated three times

            cache: typing.Dict
                cache object for storing intermediate primitive outputs

            candidate_cache: typing.Dict
                cache object for storing candidate evalueation results

        Returns:
            candidate, evaluate_value : ConfigurationPoint[T], float
        """
        if candidate is None:
            candidate = ConfigurationPoint(
                self.configuration_space, self.first_assignment())
        # first, then random, then another random
        for i in range(2):
            try:
                cand_tmp, value_tmp = self._lookup_candidate(candidate, candidate_cache)

                # if the candidate has been evaluated before and its metric value was None (
                # meaning it was not compatible with dataset), then reevaluating the candidate is
                #  redundant
                if cand_tmp is not None and value_tmp is None:
                    raise ValueError("Candidate is not compatible with the dataset")

                result = self.evaluate((candidate, cache, True if cand_tmp is None else False))

                self._push_candidate(result, candidate, candidate_cache)

                candidate.data.update(result)
                return candidate, result['test_metrics'][0]['value']
            except:
                traceback.print_exc()
                print("[ERROR] Initial Pipeline failed, Trying a random pipeline ...")
                pprint(candidate)
                print("-" * 20)
                candidate = ConfigurationPoint(self.configuration_space,
                                               self.random_assignment())
        raise ValueError("Invalid initial candidate")
        # result = self.evaluate((candidate, cache))
        # candidate.data.update(result)
        # try:
        #     result = self.evaluate(candidate)
        # except:
        #     print("***************")
        #     print("Pipeline failed")
        #     candidate = ConfigurationPoint(self.configuration_space,
        #                                    self.random_assignment())
        #     try:
        #         result = self.evaluate(candidate)
        #     except:
        #         print("Pipeline failed")
        #         candidate = ConfigurationPoint(self.configuration_space,
        #                                        self.random_assignment())
        #         result = self.evaluate(candidate)
        # return (candidate, result['test_metrics'][0]['value'])

    def search(self, candidate: ConfigurationPoint[T] = None, candidate_value: float = None, num_iter=3, max_per_dimension=10):
        for i in range(num_iter):
            candidate, candidate_value = self.search_one_iter(candidate, candidate_value, max_per_dimension=max_per_dimension)
            if candidate is None:
                return (None, None)

        return (candidate, candidate_value)


# python path of primitive, i.e. 'd3m.primitives.common_primitives.RandomForestClassifier'
PythonPath = typing.NewType('PythonPath', str)

PrimitiveDescription = typing.NewType('PrimitiveDescription', dict)


class TemplateDimensionalSearch(DimensionalSearch[PrimitiveDescription]):
    """
    Use dimensional search to find best pipeline.

    Attributes
    ----------
    template : DSBoxTemplate
        The template pipeline to be fill in
    configuration_space : ConfigurationSpace[PrimitiveDescription]
        Configuration space where values are primitive python paths
    primitive_index : typing.List[str]
        List of primitive python paths from d3m.index.search()
    train_dataset : Dataset
        The dataset to train pipeline
    test_dataset : Dataset
        The dataset to evaluate pipeline
    performance_metrics : typing.List[typing.Dict]
        Performance metrics from parse_problem_description()['problem']['performance_metrics']
    """

    def __init__(self, template: DSBoxTemplate,
                 configuration_space: ConfigurationSpace[PrimitiveDescription],
                 primitive_index: typing.List[str],
                 problem: Metadata,
                 train_dataset: Dataset,
                 test_dataset: Dataset,
                 performance_metrics: typing.List[typing.Dict],
                 output_directory: str,
                 log_dir: str,
                 num_workers: int = 0) -> None:

        # Use first metric from test

        minimize = optimization_type(performance_metrics[0]['metric']) == OptimizationType.MINIMIZE
        super().__init__(self.evaluate_pipeline, configuration_space, minimize)

        self.template: DSBoxTemplate = template
        # self.configuration_space = configuration_space
        self.primitive_index: typing.List[str] = primitive_index
        self.problem = problem
        self.train_dataset = train_dataset
        self.test_dataset = test_dataset

        self.performance_metrics = list(map(
            lambda d: {'metric': d['metric'].unparse(), 'params': d['params']},
            performance_metrics
        ))
        self.classification_metric = ('accuracy', 'precision', 'recall', 'f1', 'f1Micro', 'f1Macro', 'rocAuc', 'rocAucMicro', 'rocAucMacro')
        self.regression_metric = ('meanSquaredError', 'rootMeanSquaredError', 'rootMeanSquaredErrorAvg', 'meanAbsoluteError', 'rSquared', 'normalizedMutualInformation', 'jaccardSimilarityScore', 'precisionAtTopK', 'objectDetectionAP')

        self.output_directory = output_directory
        self.log_dir = log_dir

        self.num_workers = os.cpu_count() if num_workers == 0 else num_workers

        # print("[INFO] number of workers:", self.num_workers)

        # if not set(self.template.template_nodes.keys()) <= set(configuration_space.get_dimensions()):
        #     raise exceptions.InvalidArgumentValueError(
        #         "Not all template steps are in configuration space: {}".format(self.template.template_nodes.keys()))

    def evaluate_pipeline(self, args) -> typing.Dict:
        """
        Evaluate at configuration point.
        Note: This methods will modify the configuration point, by updating its data field.
        """
        configuration: ConfigurationPoint[PrimitiveDescription] = args[0]
        cache: typing.Dict = args[1]
        dump2disk = args[2] if len(args) == 3 else True
        print("[INFO] Worker started, id:", current_process(), ",", dump2disk)
        try:
            evaluation_result = self._evaluate(configuration, cache, dump2disk)
        except:
            traceback.print_exc()
            return None
        # configuration.data.update(new_data)
        return evaluation_result

    def _evaluate(self,
                  configuration: ConfigurationPoint,
                  cache: typing.Dict,
                  dump2disk: bool) -> typing.Dict:

        start_time = time.time()

        pipeline = self.template.to_pipeline(configuration)

        # Todo: update ResourceManager to run pipeline:  ResourceManager.add_pipeline(pipeline)
        fitted_pipeline = FittedPipeline(
            pipeline, self.train_dataset.metadata.query(())['id'], log_dir=self.log_dir, metric_descriptions=self.performance_metrics)

        fitted_pipeline.fit(cache=cache, inputs=[self.train_dataset])

        training_ground_truth = get_target_columns(self.train_dataset,
                                                   self.problem)
        training_prediction = fitted_pipeline.get_fit_step_output(
            self.template.get_output_step_number())

        results = fitted_pipeline.produce(inputs=[self.test_dataset])
        test_ground_truth = get_target_columns(self.test_dataset,
                                               self.problem)
        # Note: results == test_prediction
        test_prediction = fitted_pipeline.get_produce_step_output(
            self.template.get_output_step_number())

        training_metrics = []
        test_metrics = []
        for metric_description in self.performance_metrics:
            metricDesc = PerformanceMetric.parse(metric_description['metric'])
            metric: typing.Callable = metricDesc.get_function()
            params: typing.Dict = metric_description['params']

            # we should depends on the metrics type to evaluate prediction results
            try:
                if metric_description["metric"] in self.regression_metric:
                    training_metrics.append({
                        'metric': metric_description['metric'],
                        'value': metric(
                            training_ground_truth.iloc[:, -1].astype(float),
                            training_prediction.iloc[:, -1].astype(float),
                            **params
                        )
                    })
                    # if the test_ground_truth do not have results
                    if test_ground_truth.iloc[0, -1] == '':
                        test_ground_truth.iloc[:, -1] = 0
                    test_metrics.append({
                        'metric': metric_description['metric'],
                        'value': metric(
                            test_ground_truth.iloc[:, -1].astype(float),
                            test_prediction.iloc[:, -1].astype(float),
                            **params
                        )
                    })
                else:
                    training_metrics.append({
                        'metric': metric_description['metric'],
                        'value': metric(
                            training_ground_truth.iloc[:, -1].astype(str),
                            training_prediction.iloc[:, -1].astype(str),
                            **params
                        )
                    })
                    test_metrics.append({
                        'metric': metric_description['metric'],
                        'value': metric(
                            test_ground_truth.iloc[:, -1].astype(str),
                            test_prediction.iloc[:, -1].astype(str),
                            **params
                        )
                    })

            except:
                raise NotSupportedError(
                    '[ERROR] metric calculation failed')

        if len(test_metrics) > 0:
            fitted_pipeline.set_metric(test_metrics[0])

        data = {
            'fitted_pipeline': fitted_pipeline,
            'training_metrics': training_metrics,
            'cross_validation_metrics': fitted_pipeline.get_cross_validation_metrics(),
            'test_metrics': test_metrics,
            'total_runtime': time.time() - start_time
        }
        fitted_pipeline.auxiliary = dict(data)

        # Save results
        if self.output_directory is not None and dump2disk:
            fitted_pipeline.save(self.output_directory)
            # _logger.info("Test pickled pipeline. id: {}".format(fitted_pipeline.id))
            # self.test_pickled_pipeline(folder_loc=self.output_directory,
            #                                pipeline_id=fitted_pipeline.id,
            #                                test_metrics=test_metrics,
            #                                test_ground_truth=test_ground_truth)

        return data

    def test_pickled_pipeline(self,
                              folder_loc: str,
                              pipeline_id: str,
                              test_metrics: typing.List,
                              test_ground_truth) -> None:

        fitted_pipeline, run = FittedPipeline.load(folder_loc=folder_loc, pipeline_id=pipeline_id, log_dir=self.log_dir)
        results = fitted_pipeline.produce(inputs=[self.test_dataset])
        pipeline_pridiction = fitted_pipeline.get_produce_step_output(self.template.get_output_step_number())

        test_pipeline_metrics = list()
        for metric_description in self.performance_metrics:
            metricDesc = PerformanceMetric.parse(metric_description['metric'])
            metric: typing.Callable = metricDesc.get_function()
            params: typing.Dict = metric_description['params']

            try:
                if metric_description["metric"] in self.regression_metric:
                    # if the test_ground_truth do not have results
                    if test_ground_truth.iloc[0, -1] == '':
                        test_ground_truth.iloc[:, -1] = 0
                    test_pipeline_metrics.append({
                        'metric': metric_description['metric'],
                        'value': metric(
                            test_ground_truth.iloc[:, -1].astype(float),
                            pipeline_pridiction.iloc[:, -1].astype(float),
                            **params
                        )
                    })
                else:
                    test_pipeline_metrics.append({
                        'metric': metric_description['metric'],
                        'value': metric(
                            test_ground_truth.iloc[:, -1].astype(str),
                            pipeline_pridiction.iloc[:, -1].astype(str),
                            **params
                        )
                    })
            except:
                raise NotSupportedError(
                    '[ERROR] metric calculation failed in test pickled pipeline')

        pairs = zip(test_metrics, test_pipeline_metrics)
        if any(x != y for x, y in pairs):
            warn("[WARN] Test pickled pipeline mismatch. id: {}".format(fitted_pipeline.id))
            print(
                {
                    'id': fitted_pipeline.id,
                    'test__metric': test_metrics,
                    'pickled_pipeline__metric': test_pipeline_metrics
                }
            )
            print("\n" * 5)
            _logger.warning(
                "Test pickled pipeline mismatch. 'id': '%(id)s', 'test__metric': '%(test__metric)s', 'pickled_pipeline__metric': '%(pickled_pipeline__metric)s'.",
                {
                    'id': fitted_pipeline.id,
                    'test__metric': test_metrics,
                    'pickled_pipeline__metric': test_pipeline_metrics
                },
            )
            print("\n" * 5)
        else:
            print("\n" * 5)
            print("Pickling succeeded")
            print("\n" * 5)


PythonPathWithHyperaram = typing.Tuple[PythonPath, int, HyperparamDirective]


def generate_hyperparam_configuration_space(space: ConfigurationSpace[PythonPath]) -> ConfigurationSpace[PythonPathWithHyperaram]:
    new_space = {}
    for name in space.get_dimensions():
        values = []
        for path in space.get_values(name):
            for index, hyperparam_directive in enumerate([HyperparamDirective.DEFAULT] + [HyperparamDirective.RANDOM] * 3):
                values.append((path, index, hyperparam_directive))
        new_space[name] = values
    return SimpleConfigurationSpace(new_space)


def random_choices_without_replacement(population, weights, k=1):
    """
    Randomly sample multiple element based on weights witout replacement.
    """
    assert len(weights) == len(population)
    if k > len(population):
        k = len(population)
    weights = list(weights)
    result = []
    for index in range(k):
        cum_weights = list(accumulate(weights))
        total = cum_weights[-1]
        i = bisect.bisect(cum_weights, random.random() * total)
        result.append(population[i])
        weights[i] = 0
    return result


def accumulate(iterable, func=operator.add):
    """
    Sum all the elments
    """
    it = iter(iterable)
    try:
        total = next(it)
    except StopIteration:
        return
    yield total
    for element in it:
        total = func(total, element)
        yield total
