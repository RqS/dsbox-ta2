import os
import sys
import os.path
import uuid
import copy
import math
import json
import numpy as np
import shutil
import traceback
import inspect
import importlib
import pandas as pd


from dsbox.planner.leveltwo.l1proxy import LevelOnePlannerProxy
from dsbox.planner.leveltwo.planner import LevelTwoPlanner
from dsbox.planner.common.pipeline import Pipeline, PipelineExecutionResult
from dsbox.planner.common.resource_manager import ResourceManager
from dsbox.planner.common.problem_manager import Metric, TaskType, TaskSubType

MIN_METRICS = [Metric.MEAN_SQUARED_ERROR, Metric.ROOT_MEAN_SQUARED_ERROR, Metric.ROOT_MEAN_SQUARED_ERROR_AVG, Metric.MEAN_ABSOLUTE_ERROR, Metric.EXECUTION_TIME]
DISCRETE_METRIC = [TaskSubType.BINARY, TaskSubType.MULTICLASS, TaskSubType.MULTILABEL, TaskSubType.OVERLAPPING, TaskSubType.NONOVERLAPPING]

class Ensemble(object):
    def __init__(self, problem, max_pipelines = 100):
        self.max_pipelines = max_pipelines
        self.predictions = None #predictions # Predictions dataframe
        self.metric_values =  None #metric_values # Dictionary of metric to value
        self.pipelines = []
        self.problem = problem
        self._analyze_metrics()

    def _analyze_metrics(self):
        # *** ONLY CONSIDERS 1 METRIC ***
        #self.minimize_metric = True if self.problem.metrics[0] in MIN_METRICS else False
        self.minimize_metric = []
        for i in range(0, len(self.problem.metrics)):
            print(self.problem.metrics[i])
            self.minimize_metric.append(True if self.problem.metrics[i] in MIN_METRICS else False)
        self.discrete_metric = True if self.problem.task_subtype in DISCRETE_METRIC else False


    def greedy_add(self, pipelines, X, y, pipelines_to_add = None):
        if self.predictions is None:
            self.predictions = pd.DataFrame(index = X.index, columns = y.columns).fillna(0)     
            self.pipelines = []
    
        to_add = self.max_pipelines if pipelines_to_add is None else pipelines_to_add
        for j in range(to_add):
            best_score =  float('inf') if self.minimize_metric else 0

            # first time through
            if not self.pipelines:
                best_predictions = pipelines[0].planner_result.predictions
                best_pipeline = pipelines[0]
                best_metrics = pipelines[0].planner_result.metric_values
                best_score = np.mean(np.array([a for a in best_metrics.values()]))
            else:
                for pipeline in pipelines:

                    metric_values = {}
                    y_temp = (self.predictions.values * len(self.pipelines) + pipeline.planner_result.predictions.values) / (1.0*len(self.pipelines)+1)
                    #temp_predictions = (self.predictions[self.predictions.select_dtypes(include=['number']).columns] * len(self.pipelines) 
                    #                   + pipeline.predictions) / (len(self.pipelines)+1)

                    # check metric value binary or not
                    if self.discrete_metric:
                        y_rounded = np.rint(y_temp)
                    else:
                        y_rounded = y_temp
                    for i in range(0, len(self.problem.metrics)):
                        metric = self.problem.metrics[i]
                        fn = self.problem.metric_functions[i]
                        metric_val = self._call_function(fn, y, y_rounded)
                        if metric_val is None:
                            return None
                        metric_values[metric.name] = metric_val
                    score_improve = [v - best_metrics[k] for k, v in metric_values.items()]
                    score_improve = [score_improve[l] * (-1 if self.minimize_metric[l] else 1) for l in range(len(score_improve))]
                    score_improve = np.mean(np.array([a for a in score_improve]))
                    score = np.mean(np.array([a for a in metric_values.values()]))
                    
                    #print('Evaluating ', pipeline.primitives, score, score_improve)
                    if (score_improve >= 0):
                    #if (score > best_score and not self.minimize_metric) or (score < best_score and self.minimize_metric):
                        best_score = score
                        best_pipeline = pipeline
                        best_predictions = pd.DataFrame(y_temp, index = X.index, columns = y.columns)
                        best_metrics = metric_values
                    #pipelines.remove(pipeline)
            # evaluate / cross validate method?
            
            self.pipelines.append(best_pipeline)
            self.predictions = best_predictions
            self.metric_values = best_metrics
            print('Adding ', best_pipeline.primitives, ' to ensemble of size ', str(len(self.pipelines)), '.  Ensemble Score: ', best_score)

        #print('ENSEMBLE score: ', best_score)
        #print('ENSEMBLE pipelines: ', self.pipelines)

    def _call_function(self, scoring_function, *args):
        mod = inspect.getmodule(scoring_function)
        try:
            module = importlib.import_module(mod.__name__)
            return scoring_function(*args)
        except Exception as e:
            sys.stderr.write("ERROR _call_function %s: %s\n" % (scoring_function, e))
            traceback.print_exc()
            return None