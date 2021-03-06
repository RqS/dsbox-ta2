import os
import numpy as np
import sys
import json
import warnings

from dsbox.schema.problem_schema import TaskType, TaskSubType, Metric

# sklearn metric functions
import sklearn.metrics

PROBLEM_SCHEMA_VERSION = '3.0'
DEFAULT_PROBLEM_DOC = "problemDoc.json"

class Problem(object):
    """
    The Problem class
    It contains the problem description and pointers to datasets
    """

    prHome = None
    prDoc = None
    prID = None
    about = None

    dataset_filters = {}
    dataset_targets = {}
    task_type = None
    task_subtype = None
    metrics = []
    metric_functions = []

    splits_file = None
    predictions_file = "predictions.csv"
    scores_file = "scores.csv"

    def load_problem(self, problemPath, problemDoc=None):
        self.prHome = problemPath

        # read the schema in prHome
        # read the schema in dsHome
        if problemDoc is None:
            problemDoc = os.path.join(self.prHome, DEFAULT_PROBLEM_DOC)

        assert os.path.exists(problemDoc)
        with open(problemDoc, 'r') as f:
            self.prDoc = json.load(f)

        # make sure the versions line up
        self.about = self.prDoc["about"]
        if self.about['problemSchemaVersion'] != PROBLEM_SCHEMA_VERSION:
            warnings.warn("Problem Schema version mismatch")

        # Load bookkeeping data
        self.prID = self.about["problemID"]
        inputs = self.prDoc["inputs"]
        self.splits_file = os.path.join(self.prHome, inputs["dataSplits"]["splitsFile"])
        self.set_task_type(
            self.about.get('taskType', None),
            self.about.get('taskSubType', None)
        )
        metrics = list(map(lambda x: Metric(x["metric"]),
            inputs["performanceMetrics"]))
        self.set_metrics(metrics)
        self.set_expected_outputs(self.prDoc.get("expectedOutputs", {}))

        # Load the dataset targets and filters
        for dsitem in self.prDoc["inputs"]["data"]:
            dsid = dsitem.get("datasetID")
            dstargets = dsitem.get("targets")
            self.dataset_targets[dsid] = dstargets
            dsfilters = dsitem.get("filters", {})
            self.dataset_filters[dsid] = dsfilters

    """
    Set the task type and task subtype
    """
    def set_task_type(self, task_type, task_subtype=None):
        self.task_type = TaskType(task_type)
        self.task_subtype = None
        if task_subtype is not None:
            self.task_subtype = TaskSubType(task_subtype)

    """
    Set the output file
    """
    def set_expected_outputs(self, op):
        self.predictions_file = op.get("predictionsFile", "predictions.csv")
        self.scores_file = op.get("scoresFile", "scores.csv")

    """
    Set the metrics
    """
    def set_metrics(self, metrics):
        self.metrics = metrics
        self.set_metric_functions()

    def set_metric_functions(self):
        self.metric_functions = []
        for metric in self.metrics:
            self.metric_functions.append(self._get_metric_function(metric))

    def get_problem_id(self):
        return self.prID

    def get_dataset_ids(self):
        return list(self.dataset_filters.keys())

    def _get_metric_function(self, metric):
        if metric==Metric.ACCURACY:
            return sklearn.metrics.accuracy_score
        elif metric==Metric.F1:
            # Default f1 assumes average='binary', i.e. two types of label
            # FIXME: For now use micro for non-binary
            if self.task_subtype==TaskSubType.BINARY:
                return sklearn.metrics.f1_score
            else:
                return self.f1_micro
        elif metric==Metric.F1_MICRO:
            return self.f1_micro
        elif metric==Metric.F1_MACRO:
            return self.f1_macro
        elif metric==Metric.ROC_AUC:
            return sklearn.metrics.roc_auc_score
        elif metric==Metric.ROC_AUC_MICRO:
            return self.roc_auc_micro
        elif metric==Metric.ROC_AUC_MACRO:
            return self.roc_auc_macro
        elif metric==Metric.MEAN_SQUARED_ERROR:
            return sklearn.metrics.mean_squared_error
        elif metric==Metric.ROOT_MEAN_SQUARED_ERROR:
            return self.root_mean_squared_error
        elif metric==Metric.ROOT_MEAN_SQUARED_ERROR_AVG:
            return self.root_mean_squared_error
        elif metric==Metric.MEAN_ABSOLUTE_ERROR:
            return sklearn.metrics.mean_absolute_error
        elif metric==Metric.R_SQUARED:
            return sklearn.metrics.r2_score
        elif metric==Metric.NORMALIZED_MUTUAL_INFORMATION:
            return sklearn.metrics.normalized_mutual_info_score
        elif metric==Metric.JACCARD_SIMILARITY_SCORE:
            return sklearn.metrics.jaccard_similarity_score
        elif metric==Metric.PRECISION_AT_TOP_K:
            return self.precision_at_top_K
        else:
            sys.stderr.write("ERROR Unknown metric : {}\n".format(metric))
            return None

    ''' Custom Metric Functions '''
    def f1_micro(self, y_true, y_pred):
        return sklearn.metrics.f1_score(y_true, y_pred, average="micro")

    def f1_macro(self, y_true, y_pred):
        return sklearn.metrics.f1_score(y_true, y_pred, average="macro")

    def roc_auc_micro(self, y_true, y_pred):
        return sklearn.metrics.roc_auc_score(y_true, y_pred, average="micro")

    def roc_auc_macro(self, y_true, y_pred):
        return sklearn.metrics.roc_auc_score(y_true, y_pred, average="macro")

    def root_mean_squared_error(self, y_true, y_pred):
        import math
        return math.sqrt(sklearn.metrics.mean_squared_error(y_true, y_pred))


    # From: https://gitlab.datadrivendiscovery.org/MIT-LL/d3m_data_supply/blob/shared/documentation/problemSchema.md
    def precision_at_top_K(gt, preds, K=20):
        """
        This function examines the first K entries of a
        ground truth vector (gt) and predicted labels (preds)
        and determines how many values are shared between them.
        The result is then scaled by K to get the accuracy at top K.

        Parameters:
        -----------
        gt: 1d array-like
            Array of ground truth labels.

        preds: 1d array-like
            Array of predicted labels.

        K: int, 20 by default
            The number of samples to use when computing the accuracy.

        Returns:
        --------
        prec_at_top_K: float
            The number of labels shared between the ground truth and
            the predictions divided by K.


        Example:
            >>> gt = [0, 1, 2, 3, 4]
            >>> pred = [1, 3, 2, 4, 0]

            >>> precision_at_top_K(gt, pred, K=3)
            0.667

            >>> precision_at_top_K(gt, pred, K=4)
            0.75
        """

        gt = gt[0:K]
        preds = preds[0:K]
        prec_at_top_K = np.float(len(np.intersect1d(gt, preds))) / K
        return prec_at_top_K
