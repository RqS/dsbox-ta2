import os
import pdb
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ta3ta2_api = os.path.abspath(os.path.join(
    CURRENT_DIR, '..', '..', '..', '..', 'ta3ta2-api'))
print(ta3ta2_api)
sys.path.append(ta3ta2_api)


import d3m.metadata.problem as d3m_problem

import core_pb2
import core_pb2_grpc
import logging
from google.protobuf.timestamp_pb2 import Timestamp
import random
import string

from pprint import pprint

import problem_pb2
import value_pb2


# import autoflowconfig
from core_pb2 import HelloResponse
from core_pb2 import SearchSolutionsResponse
from core_pb2 import GetSearchSolutionsResultsResponse
from core_pb2 import Progress
from core_pb2 import ProgressState
from core_pb2 import ScoreSolutionResponse
from core_pb2 import GetScoreSolutionResultsResponse
from core_pb2 import Score
from core_pb2 import EndSearchSolutionsResponse
from core_pb2 import ScoringConfiguration
from core_pb2 import EvaluationMethod

from problem_pb2 import ProblemPerformanceMetric
from problem_pb2 import PerformanceMetric
from problem_pb2 import ProblemTarget

from value_pb2 import Value

from dsbox.controller.controller import Controller

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s -- %(message)s')
_logger = logging.getLogger(__name__)

# problem.proto and d3m.metadata.problem have different schemes for metrics
# Mapping needed for v2018.4.18, but not for later versions
pb2_to_d3m_metric = {
    0 : None,  # METRIC_UNDEFINED
    1 : d3m_problem.PerformanceMetric.ACCURACY,
    2 : None,  # PRECISION
    3 : None,  # RECALL
    4 : d3m_problem.PerformanceMetric.F1,
    5 : d3m_problem.PerformanceMetric.F1_MICRO,
    6 : d3m_problem.PerformanceMetric.F1_MACRO,
    7 : d3m_problem.PerformanceMetric.ROC_AUC,
    8 : d3m_problem.PerformanceMetric.ROC_AUC_MICRO,
    9 : d3m_problem.PerformanceMetric.ROC_AUC_MACRO,
    10 : d3m_problem.PerformanceMetric.MEAN_SQUARED_ERROR,
    11 : d3m_problem.PerformanceMetric.ROOT_MEAN_SQUARED_ERROR,
    12 : d3m_problem.PerformanceMetric.ROOT_MEAN_SQUARED_ERROR_AVG,
    13 : d3m_problem.PerformanceMetric.MEAN_ABSOLUTE_ERROR,
    14 : d3m_problem.PerformanceMetric.R_SQUARED,
    15 : d3m_problem.PerformanceMetric.NORMALIZED_MUTUAL_INFORMATION,
    16 : d3m_problem.PerformanceMetric.JACCARD_SIMILARITY_SCORE,
    17 : d3m_problem.PerformanceMetric.PRECISION_AT_TOP_K,
#    18 : d3m_problem.PerformanceMetric.OBJECT_DETECTION_AVERAGE_PRECISION
}


# The output of this function should be the same sas the output for
# d3m/metadata/problem.py:parse_problem_description
def problem_to_dict(problem) -> dict:
    description = {
        'schema': d3m_problem.PROBLEM_SCHEMA_VERSION,
        'problem': {
            'id': problem.problem.id,
            # "problemVersion" is required by the schema, but we want to be compatible with problem
            # descriptions which do not adhere to the schema.
            'version': problem.problem.version,
            'name': problem.problem.name,
            'task_type': d3m_problem.TaskType(problem.problem.task_type),
            'task_subtype': d3m_problem.TaskSubtype(problem.problem.task_subtype)
        },
        # 'outputs': {
        #     'predictions_file': problem_doc['expectedOutputs']['predictionsFile'],
        # }
    }

    performance_metrics = []
    for metrics in problem.problem.performance_metrics:
        if metrics.metric==0:
            d3m_metric = None
        else:
            d3m_metric = d3m_problem.PerformanceMetric(metrics.metric)
        params = {}
        if d3m_metric == d3m_problem.PerformanceMetric.F1:
            if metrics.pos_label is None:
                params['pos_label'] = '1'
            else:
                params['pos_label'] = metrics.pos_label
        if metrics.k is not None:
            params['k'] = metrics.k
        performance_metrics.append ({
            'metric' : d3m_metric,
            'params' : params
        })
    description['problem']['performance_metrics'] = performance_metrics

    inputs = []
    for input in problem.inputs:
        dataset_id = input.dataset_id
        for target in input.targets:
            targets = []
            targets.append({
                'target_index': target.target_index,
                'resource_id': target.resource_id,
                'column_index': target.column_index,
                'column_name': target.column_name,
                'clusters_number': target.clusters_number
            })
        inputs.append({
            'dataset_id': dataset_id,
            'targets': targets
        })
    description['inputs'] = inputs

    return description

'''
This class implements the CoreServicer base class. The CoreServicer defines the methods that must be supported by a
TA2 server implementation. The CoreServicer class is generated by grpc using the core.proto descriptor file. See:
https://gitlab.com/datadrivendiscovery/ta3ta2-api.
'''
class TA2Servicer(core_pb2_grpc.CoreServicer):

    '''
    The __init__ method is used to establish the underlying TA2 libraries to service requests from the TA3 system.
    '''
    def __init__(self, libdir):
        self.log_msg("Init invoked")
        self.libdir = libdir
        self.controller = Controller(libdir)


    '''
    Hello call
    Non streaming call
    '''
    def Hello(self, request, context):
        self.log_msg(msg="Hello invoked")
        # TODO: Figure out what we should be sending back to TA3 here.
        return HelloResponse(user_agent="SRI",
                             version="1.3",
                             allowed_value_types="",
                             supported_extensions="")


    '''
    Search Solutions call
    Non streaming
    '''
    def SearchSolutions(self, request, context):
        self.log_msg(msg="SearchSolutions invoked")

        problem_description = problem_to_dict(request.problem)

        # Although called uri, it's just a filepath to datasetDoc.json
        dataset_uri = request.inputs[0].dataset_uri

        config_dict = {
            'problem' : problem_description,
            'dataset_schema': dataset_uri,
            'timeout' : request.time_bound
        }

        pprint(config_dict)

        self.controller.initialize_from_ta3(config_dict)

        status = self.controller.train()

        return SearchSolutionsResponse(search_id=self.generateId())


    '''
    Get Search Solutions Results call
    Streams response to TA3
    '''
    def GetSearchSolutionsResults(self, request, context):
        self.log_msg(msg="GetSearchSolutionsResults invoked with search_id: " + request.search_id)
        # TODO: Read the pipelines we generated and munge them into the response for TA3
        timestamp = Timestamp()
        searchSolutionsResults = []

        score = self.controller.candidate_value['validation_metrics']['value']
        scoring_config = ScoringConfiguration(
            method=EvaluationMethod.HOLDOUT,
            train_test_ratio=5,
            random_seed=4676,
            stratified=True
        )
        targets = []
        targets.append(ProblemTarget(
            int32 target_index = 1;
            string resource_id = 2;
            int32 column_index = 3;
            string column_name = 4;
            int32 clusters_number = 5;
        )
        )

        score = Score(
            metric=ProblemPerformanceMetric(
                metric=PerformanceMetric(
                    self.controller.candidate_value['validation_metrics']['metric'])
                k=0,
                pos_label = '')
            fold=0,
            targets=targets
        )
        scores = SearchSolutionScore(
            scoring_configuration=scoring_config,
            scores=[score]
        )
        searchSolutionsResults.append(GetSearchSolutionsResultsResponse(
            progress=Progress(state=core_pb2.COMPLETED,
            status="Done",
            start=timestamp.GetCurrentTime(),
            end=timestamp.GetCurrentTime()),
            done_ticks=0, # TODO: Figure out how we want to support this
            all_ticks=0, # TODO: Figure out how we want to support this
            solution_id="HIDOEI8973", # TODO: Populate this with the pipeline id
            internal_score=0,
            # scores=None # Optional so we will not tackle it until needed
            scores=scores
        ))
        # Add a second result to test streaming responses
        searchSolutionsResults.append(GetSearchSolutionsResultsResponse(
            progress=Progress(state=core_pb2.RUNNING,
            status="Done",
            start=timestamp.GetCurrentTime(),
            end=timestamp.GetCurrentTime()),
            done_ticks=0,
            all_ticks=0,
            solution_id="JIOEPB343", # TODO: Populate this with the pipeline id
            internal_score=0,
            scores=None
        ))
        for solution in searchSolutionsResults:
            yield solution


    '''
    Get the Score Solution request_id associated with the supplied solution_id
    Non streaming
    '''
    def ScoreSolution(self, request, context):
        self.log_msg(msg="ScoreSolution invoked with solution_id: " + request.solution_id)

        return ScoreSolutionResponse(
            # Generate valid request id 22 characters long for TA3 tracking
            request_id=self.generateId()
        )


    '''
    Get Score Solution Results call
    Streams response to TA3
    '''
    def GetScoreSolutionResults(self, request, context):
        self.log_msg(msg="GetScoreSolutionResults invoked with request_id: " + request.request_id)

        scoreSolutionResults = []
        timestamp = Timestamp()
        scoreSolutionResults.append(
            GetScoreSolutionResultsResponse(
            progress=Progress(state=core_pb2.COMPLETED,
                              status="Good",
                              start=timestamp.GetCurrentTime(),
                              end=timestamp.GetCurrentTime()),
            scores=[Score(metric=ProblemPerformanceMetric(metric=problem_pb2.ACCURACY,
                                            k = 0,
                                            pos_label="0"),
                          fold=0,
                          targets=[ProblemTarget(target_index=0,
                                           resource_id="0",
                                           column_index=0,
                                           column_name="0",
                                           clusters_number=0)],
                          value=Value(double=0.8))]
        ))
        scoreSolutionResults.append(GetScoreSolutionResultsResponse(
            progress=Progress(state=core_pb2.PENDING,
                              status="Good",
                              start=timestamp.GetCurrentTime(),
                              end=timestamp.GetCurrentTime()
        )))
        for score in scoreSolutionResults:
            yield score


    '''
    End the solution search process with the supplied search_id
    Non streaming
    '''
    def EndSearchSolutions(self, request, context):
        self.log_msg(msg="EndSearchSolutions invoked with search_id: " + request.search_id)

        return EndSearchSolutionsResponse()


    def GetProduceSolutionResults(self, request, context):
        pass


    def SolutionExport(self, request, context):
        pass


    def GetFitSolutionResults(self, request, context):
        pass


    def StopSearchSolutions(self, request, context):
        pass


    def ListPrimitives(self, request, context):
        pass


    def ProduceSolution(self, request, context):
        pass


    def FitSolution(self, request, context):
        pass


    def UpdateProblem(self, request, context):
        pass


    def DescribeSolution(self, request, context):
        pass


    '''
    Handy method for generating pipeline trace logs
    '''
    def log_msg(self, msg):
        msg = str(msg)
        for line in msg.splitlines():
            _logger.info("    | %s" % line)
        _logger.info("    \\_____________")


    '''
    Convenience method for generating 22 character id's
    '''
    def generateId(self):
        return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(22))
