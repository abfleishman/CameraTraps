########
#
# postprocess_batch_results.py
#
# Given a .json or .csv file representing the output from the batch API, do one or more of
# the following:
#
# * Evaluate detector precision/recall, optionally rendering results (requires ground truth)
#
# * Sample true/false positives/negatives and render to html (requires ground truth)
#
# * Sample detections/non-detections and render to html (when ground truth isn't available)
#
# Upcoming improvements:
#
# * Elimination of "suspicious detections", i.e. detections repeated numerous times with
#   unrealistically limited movement... this is implemented, but currently as a step that
#   runs *before* this script.  See find_problematic_detections.py.
#
# * Support for accessing blob storage directly (currently images are accessed by
#   file paths, so images in Azure blobs should be accessed by mounting the
#   containers).
#
########


#%% Constants and imports

import argparse
import os
import sys
import collections
import io
import warnings
import copy
import time
from multiprocessing.pool import ThreadPool    
from enum import IntEnum
import errno
import uuid
            
import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_curve, confusion_matrix, average_precision_score
from tqdm import tqdm
import humanfriendly

# Assumes ai4eutils is on the python path
# https://github.com/Microsoft/ai4eutils
from write_html_image_list import write_html_image_list

# Assumes the cameratraps repo root is on the path
import visualization.visualization_utils as vis_utils
from data_management.cct_json_utils import CameraTrapJsonUtils, IndexedJsonDb
from api.batch_processing.postprocessing.load_api_results import load_api_results
from ct_utils import args_to_object

warnings.filterwarnings("ignore", "(Possibly )?corrupt EXIF data", UserWarning)


#%% Options

DEFAULT_NEGATIVE_CLASSES = ['empty']
DEFAULT_UNKNOWN_CLASSES = ['unknown', 'unlabeled', 'ambiguous']


def has_overlap(set1, set2):
    ''' Helper function that checks whether two sets overlap '''
    
    return len(set(set1) & set(set2)) > 0


# Make sure there is no overlap between the two sets, because this will cause
# issues in the code
assert not has_overlap(DEFAULT_NEGATIVE_CLASSES, DEFAULT_UNKNOWN_CLASSES), \
        'Default negative and unknown classes cannot overlap.'


class PostProcessingOptions:

    ### Required inputs

    api_output_file = ''
    image_base_dir = ''
    ground_truth_json_file = ''
    output_dir = ''

    ### Options

    ground_truth_json_file = ''

    negative_classes = DEFAULT_NEGATIVE_CLASSES
    unlabeled_classes = DEFAULT_UNKNOWN_CLASSES

    confidence_threshold = 0.85

    # Used for summary statistics only
    target_recall = 0.9

    # Number of images to sample, -1 for "all images"
    num_images_to_sample = 500 # -1

    # Random seed for sampling, or None
    sample_seed = 0 # None

    viz_target_width = 800

    sort_html_by_filename = True
    
    # Optionally replace one or more strings in filenames with other strings;
    # this is useful for taking a set of results generated for one folder structure
    # and applying them to a slightly different folder structure.
    api_output_filename_replacements = {}
    ground_truth_filename_replacements = {}

    # Allow bypassing API output loading when operating on previously-loaded results
    api_detection_results = None
    api_other_fields = None
    
    # Should we also split out a separate report about the detections that were
    # just below our main confidence threshold?
    #
    # Currently only supported when ground truth is unavailable
    include_almost_detections = False
    almost_detection_confidence_threshold = 0.75

    # Control rendering parallelization
    parallelize_rendering_n_cores = 100
    parallelize_rendering = False
    
    
class PostProcessingResults:

    output_html_file = ''
    api_detection_results = None
    api_other_fields = None
        

##%% Helper classes and functions

# Flags used to mark images as positive or negative for P/R analysis (according
# to ground truth and/or detector output)
class DetectionStatus(IntEnum):

    # This image is a negative
    DS_NEGATIVE = 0

    # This image is a positive
    DS_POSITIVE = 1

    # Anything greater than this isn't clearly positive or negative
    DS_MAX_DEFINITIVE_VALUE = DS_POSITIVE

    # This image has annotations suggesting both negative and positive
    DS_AMBIGUOUS = 2

    # This image is not annotated or is annotated with 'unknown', 'unlabeled', ETC.
    DS_UNKNOWN = 3

    # This image has not yet been assigned a state
    DS_UNASSIGNED = 4

    # In some analyses, we add an additional class that lets us look at detections just below
    # our main confidence threshold
    DS_ALMOST = 5
        

def mark_detection_status(indexed_db, negative_classes=DEFAULT_NEGATIVE_CLASSES,
                          unknown_classes=DEFAULT_UNKNOWN_CLASSES):
    """
    For each image in indexed_db.db['images'], add a '_detection_status' field
    to indicate whether to treat this image as positive, negative, ambiguous,
    or unknown.

    Makes modifications in-place.

    returns (n_negative, n_positive, n_unknown, n_ambiguous)
    """
    negative_classes = set(negative_classes)
    unknown_classes = set(unknown_classes)

    # Counter for the corresponding fields of class (actually enum) DetectionStatus
    n_unknown = 0
    n_ambiguous = 0
    n_positive = 0
    n_negative = 0

    print('Preparing ground-truth annotations')
    db = indexed_db.db
    for im in tqdm(db['images']):

        image_id = im['id']
        annotations = indexed_db.image_id_to_annotations[image_id]
        image_categories = [ann['category_id'] for ann in annotations]
        image_category_names = set([indexed_db.cat_id_to_name[cat] for cat in image_categories])

        # Check if image has unassigned-type labels
        image_has_unknown_labels = has_overlap(image_category_names, unknown_classes)
        # Check if image has negative-type labels
        image_has_negative_labels = has_overlap(image_category_names, negative_classes)
        # Check if image has positive labels
        # i.e. if we remove negative and unknown labels from image_category_names, then
        # there are still labels left
        image_has_positive_labels = 0 < len(image_category_names - unknown_classes - negative_classes)

        # If there are no image annotations, the result is unknonw
        if len(image_categories) == 0:

            n_unknown += 1
            im['_detection_status'] = DetectionStatus.DS_UNKNOWN

        # If the image has more than one type of labels, it's ambiguous
        # note: booleans get automatically converted to 0/1, hence we can use the sum
        elif image_has_unknown_labels + image_has_negative_labels + image_has_positive_labels > 1:

            n_ambiguous += 1
            im['_detection_status'] = DetectionStatus.DS_AMBIGUOUS

        # After the check above, we can be sure it's only one of positive, negative, or unknown
        # Important: do not merge the following 'unknown' branch with the first 'unknown' branch
        # above, where we were testing 'if len(image_categories) == 0'
        #
        # If the image has only unknown labels
        elif image_has_unknown_labels:

            n_unknown += 1
            im['_detection_status'] = DetectionStatus.DS_UNKNOWN

        # If the image has only negative labels
        elif image_has_negative_labels:

            n_negative += 1
            im['_detection_status'] = DetectionStatus.DS_NEGATIVE

        # If the images has only positive labels
        elif image_has_positive_labels:

            n_positive += 1
            im['_detection_status'] = DetectionStatus.DS_POSITIVE

            # Annotate the category, if it is unambiguous
            if len(image_category_names) == 1:
                im['_unambiguous_category'] = list(image_category_names)[0]

        else:
            raise Exception('Invalid state, please check the code for bugs')

    return n_negative, n_positive, n_unknown, n_ambiguous


def render_bounding_boxes(image_base_dir, image_relative_path, display_name, detections, res,
                          detection_categories_map=None, classification_categories_map=None, options=None):
        """
        Renders detection bounding boxes on a single image.  Returns the html info struct
        for this image in the form that's used for write_html_image_list.
        """
        
        if options is None:
            options = PostProcessingOptions()

        # Leaving code in place for reading from blob storage, may support this
        # in the future.
        """
        stream = io.BytesIO()
        _ = blob_service.get_blob_to_stream(container_name, image_id, stream)
        image = Image.open(stream).resize(viz_size)  # resize is to display them in this notebook or in the HTML more quickly
        """

        image_full_path = os.path.join(image_base_dir, image_relative_path)
        
        # isfile() is slow when mounting remote directories; much faster to just try/except
        # on the image open.
        if False:
            if not os.path.isfile(image_full_path):
                print('Warning: could not find image file {}'.format(image_full_path))
                return ''
        
        try:
            image = vis_utils.open_image(image_full_path)
        except:
            print('Warning: could not open image file {}'.format(image_full_path))
            return ''
        
        image = vis_utils.resize_image(image, options.viz_target_width)

        vis_utils.render_detection_bounding_boxes(detections, image,
                                                  label_map=detection_categories_map,
                                                  classification_label_map=classification_categories_map,
                                                  confidence_threshold=options.confidence_threshold,
                                                  thickness=4)

        # Render images to a flat folder... we can use os.sep here because we've
        # already normalized paths
        sample_name = res + '_' + image_relative_path.replace(os.sep, '~')

        try:
            image.save(os.path.join(options.output_dir, res, sample_name))
        except OSError as e:
            if e.errno == errno.ENAMETOOLONG:
                sample_name = res + '_' + str(uuid.uuid4()) + '.jpg'
                image.save(os.path.join(options.output_dir, res, sample_name))
            else:
                raise

        # Use slashes regardless of os
        file_name = '{}/{}'.format(res, sample_name)

        return {
            'filename': file_name,
            'title': display_name,
            'textStyle': 'font-family:verdana,arial,calibri;font-size:80%;text-align:left;margin-top:20;margin-bottom:5'
        }


def prepare_html_subpages(images_html, output_dir, options=None):
    """
    Write out a series of html image lists, e.g. the fp/tp/fn/tn pages.

    image_html is a dictionary mapping an html page name (e.g. "fp") to a list
    of image structs friendly to write_html_image_list
    """
    if options is None:
            options = PostProcessingOptions()

    # Count items in each category
    image_counts = {}
    for res, array in images_html.items():
        image_counts[res] = len(array)

    # Optionally sort by filename before writing to html
    if options.sort_html_by_filename:
        images_html_sorted = {}
        for res, array in images_html.items():
            sorted_array = sorted(array, key=lambda x: x['filename'])
            images_html_sorted[res] = sorted_array
        images_html = images_html_sorted

    # Write the individual HTML files
    for res, array in images_html.items():
        write_html_image_list(
            filename=os.path.join(output_dir, '{}.html'.format(res)),
            images=array,
            options={
                'headerHtml': '<h1>{}</h1>'.format(res.upper())
            })

    return image_counts


#%% Main function

def process_batch_results(options):

    ppresults = PostProcessingResults()
    
    ##%% Expand some options for convenience

    output_dir = options.output_dir


    ##%% Prepare output dir

    os.makedirs(output_dir, exist_ok=True)


    ##%% Load ground truth if available

    ground_truth_indexed_db = None
    
    if options.ground_truth_json_file and len(options.ground_truth_json_file) > 0:

        ground_truth_indexed_db = IndexedJsonDb(options.ground_truth_json_file, b_normalize_paths=True,
                                                filename_replacements=options.ground_truth_filename_replacements)

        # Mark images in the ground truth as positive or negative
        n_negative, n_positive, n_unknown, n_ambiguous = mark_detection_status(ground_truth_indexed_db,
            negative_classes=options.negative_classes, unknown_classes=options.unlabeled_classes)
        print('Finished loading and indexing ground truth: {} negative, {} positive, {} unknown, {} ambiguous'.format(
                n_negative, n_positive, n_unknown, n_ambiguous))


    ##%% Load detection results

    if options.api_detection_results is None:
        detection_results, other_fields = load_api_results(options.api_output_file,
                                                 normalize_paths=True,
                                                 filename_replacements=options.api_output_filename_replacements)
        ppresults.api_detection_results = detection_results
        ppresults.api_other_fields = other_fields
        
    else:
        print('Bypassing detection results loading...')
        assert options.api_other_fields is not None
        detection_results = options.api_detection_results
        other_fields = options.api_other_fields
        
    detection_categories_map = other_fields['detection_categories']
    if 'classification_categories' in other_fields:
        classification_categories_map = other_fields['classification_categories']
    else:
        classification_categories_map = {}

    # Add a column (pred_detection_label) to indicate predicted detection status, not separating out the classes    
    if options.include_almost_detections:
        detection_results['pred_detection_label'] = DetectionStatus.DS_ALMOST
        confidences = detection_results['max_detection_conf']
        detection_results.loc[confidences >= options.confidence_threshold,'pred_detection_label'] = DetectionStatus.DS_POSITIVE
        detection_results.loc[confidences < options.almost_detection_confidence_threshold,'pred_detection_label'] = DetectionStatus.DS_NEGATIVE        
    else:
        detection_results['pred_detection_label'] = \
        np.where(detection_results['max_detection_conf'] >= options.confidence_threshold,
                 DetectionStatus.DS_POSITIVE, DetectionStatus.DS_NEGATIVE)
        
    n_positives = sum(detection_results['pred_detection_label'] == DetectionStatus.DS_POSITIVE)
    print('Finished loading and preprocessing {} rows from detector output, predicted {} positives'.format(
            len(detection_results), n_positives))

    if options.include_almost_detections:
        n_almosts = sum(detection_results['pred_detection_label'] == DetectionStatus.DS_ALMOST)
        print('...and {} almost-positives'.format(n_almosts))
    

    ##%% If we have ground truth, remove images we can't match to ground truth

    if ground_truth_indexed_db is not None:

        b_match = [False] * len(detection_results)

        detector_files = detection_results['file'].tolist()

        # fn = detector_files[0]; print(fn)
        for i_fn, fn in enumerate(detector_files):

            # assert fn in ground_truth_indexed_db.filename_to_id, 'Could not find ground truth for row {} ({})'.format(i_fn,fn)
            if fn in ground_truth_indexed_db.filename_to_id:
                b_match[i_fn] = True

        print('Confirmed filename matches to ground truth for {} of {} files'.format(sum(b_match), len(detector_files)))

        detection_results = detection_results[b_match]
        detector_files = detection_results['file'].tolist()

        assert len(detector_files) > 0, 'No detection files available, possible ground truth path issue?'
        
        print('Trimmed detection results to {} files'.format(len(detector_files)))

    
    ##%% Sample images for visualization

    images_to_visualize = detection_results

    if options.num_images_to_sample > 0 and options.num_images_to_sample <= len(detection_results):

        images_to_visualize = images_to_visualize.sample(options.num_images_to_sample, random_state=options.sample_seed)


    output_html_file = ''

    style_header = """<head>
        <style type="text/css">
        <!--
        a { text-decoration:none; }
        body { font-family:segoe ui, calibri, "trebuchet ms", verdana, arial, sans-serif; }
        div.contentdiv { margin-left:20px; }
        -->
        </style>
        </head>"""

        
    ##%% Fork here depending on whether or not ground truth is available

    # If we have ground truth, we'll compute precision/recall and sample tp/fp/tn/fn.
    #
    # Otherwise we'll just visualize detections/non-detections.

    if ground_truth_indexed_db is not None:

        ##%% Detection evaluation: compute precision/recall

        # numpy array of detection probabilities
        p_detection = detection_results['max_detection_conf'].values
        n_detections = len(p_detection)

        # numpy array of bools (0.0/1.0), and -1 as null value
        gt_detections = np.zeros(n_detections, dtype=float)

        for i_detection, fn in enumerate(detector_files):
            image_id = ground_truth_indexed_db.filename_to_id[fn]
            image = ground_truth_indexed_db.image_id_to_image[image_id]
            detection_status = image['_detection_status']

            if detection_status == DetectionStatus.DS_NEGATIVE:
                gt_detections[i_detection] = 0.0
            elif detection_status == DetectionStatus.DS_POSITIVE:
                gt_detections[i_detection] = 1.0
            else:
                gt_detections[i_detection] = -1.0

        # Don't include ambiguous/unknown ground truth in precision/recall analysis
        b_valid_ground_truth = gt_detections >= 0.0

        p_detection_pr = p_detection[b_valid_ground_truth]
        gt_detections_pr = gt_detections[b_valid_ground_truth]

        print('Including {} of {} values in p/r analysis'.format(np.sum(b_valid_ground_truth),
              len(b_valid_ground_truth)))

        precisions, recalls, thresholds = precision_recall_curve(gt_detections_pr, p_detection_pr)

        # For completeness, include the result at a confidence threshold of 1.0
        thresholds = np.append(thresholds, [1.0])

        precisions_recalls = pd.DataFrame(data={
                'confidence_threshold': thresholds,
                'precision': precisions,
                'recall': recalls
            })

        # Compute and print summary statistics
        average_precision = average_precision_score(gt_detections_pr, p_detection_pr)
        print('Average precision: {:.1%}'.format(average_precision))

        # Thresholds go up throughout precisions/recalls/thresholds; find the last
        # value where recall is at or above target.  That's our precision @ target recall.
        target_recall = 0.9
        b_above_target_recall = np.where(recalls >= target_recall)
        if not np.any(b_above_target_recall):
            precision_at_target_recall = 0.0
        else:
            i_target_recall = np.argmax(b_above_target_recall)
            precision_at_target_recall = precisions[i_target_recall]
        print('Precision at {:.1%} recall: {:.1%}'.format(target_recall, precision_at_target_recall))

        cm = confusion_matrix(gt_detections_pr, np.array(p_detection_pr) > options.confidence_threshold)

        # Flatten the confusion matrix
        tn, fp, fn, tp = cm.ravel()

        precision_at_confidence_threshold = tp / (tp + fp)
        recall_at_confidence_threshold = tp / (tp + fn)
        f1 = 2.0 * (precision_at_confidence_threshold * recall_at_confidence_threshold) / \
            (precision_at_confidence_threshold + recall_at_confidence_threshold)

        print('At a confidence threshold of {:.1%}, precision={:.1%}, recall={:.1%}, f1={:.1%}'.format(
                options.confidence_threshold, precision_at_confidence_threshold, recall_at_confidence_threshold, f1))

        ##%% Collect classification results, if they exist
        
        classifier_accuracies = []
        
        # Mapping of classnames to idx for the confusion matrix.
        #
        # The lambda is actually kind of a hack, because we use assume that
        # the following code does not reassign classname_to_idx
        classname_to_idx = collections.defaultdict(lambda: len(classname_to_idx))
        
        # Confusion matrix as defaultdict of defaultdict
        #
        # Rows / first index is ground truth, columns / second index is predicted category
        classifier_cm = collections.defaultdict(lambda: collections.defaultdict(lambda: 0))
        
        # iDetection = 0; fn = detector_files[iDetection]; print(fn)
        assert len(detector_files) == len(detection_results)
        for iDetection,fn in enumerate(detector_files):
            
            image_id = ground_truth_indexed_db.filename_to_id[fn]
            image = ground_truth_indexed_db.image_id_to_image[image_id]
            detections = detection_results['detections'].iloc[iDetection]
            pred_class_ids = [det['classifications'][0][0] \
                for det in detections if 'classifications' in det.keys()]
            pred_classnames = [classification_categories_map[pd] for pd in pred_class_ids]

            # If this image has classification predictions, and an unambiguous class
            # annotated, and is a positive image...
            if len(pred_classnames) > 0 \
                    and '_unambiguous_category' in image.keys() \
                    and image['_detection_status'] == DetectionStatus.DS_POSITIVE:
                        
                # The unambiguous category, we make this a set for easier handling afterward
                gt_categories = set([image['_unambiguous_category']])
                pred_categories = set(pred_classnames)
                
                # Compute the accuracy as intersection of union,
                # i.e. (# of categories in both prediciton and GT)
                #      divided by (# of categories in either prediction or GT
                #
                # In case of only one GT category, the result will be 1.0, if
                # prediction is one category and this category matches GT
                #
                # It is 1.0/(# of predicted top-1 categories), if the GT is
                # one of the predicted top-1 categories.
                #
                # It is 0.0, if none of the predicted categories is correct
                
                classifier_accuracies.append(
                    len(gt_categories & pred_categories)
                    / len(gt_categories | pred_categories)
                )
                image['_classification_accuracy'] = classifier_accuracies[-1]
                
                # Distribute this accuracy across all predicted categories in the
                # confusion matrix
                assert len(gt_categories) == 1
                gt_class_idx = classname_to_idx[list(gt_categories)[0]]
                for pred_category in pred_categories:
                    pred_class_idx = classname_to_idx[pred_category]
                    classifier_cm[gt_class_idx][pred_class_idx] += 1

        # ...for each file in the detection results
        
        # If we have classification results
        if len(classifier_accuracies) > 0:
            
            # Build confusion matrix as array from classifier_cm
            all_class_ids = sorted(classname_to_idx.values())
            classifier_cm_array = np.array(
                [[classifier_cm[r_idx][c_idx] for c_idx in all_class_ids] for r_idx in all_class_ids], dtype=float)
            classifier_cm_array /= (classifier_cm_array.sum(axis=1, keepdims=True) + 1e-7)

            # Print some statistics
            print("Finished computation of {} classification results".format(len(classifier_accuracies)))
            print("Mean accuracy: {}".format(np.mean(classifier_accuracies)))

            # Prepare confusion matrix output
            
            # Get confusion matrix as string
            sio = io.StringIO()
            np.savetxt(sio, classifier_cm_array * 100, fmt='%5.1f')
            cm_str = sio.getvalue()
            # Get fixed-size classname for each idx
            idx_to_classname = {v:k for k,v in classname_to_idx.items()}
            classname_list = [idx_to_classname[idx] for idx in sorted(classname_to_idx.values())]
            classname_headers = ['{:<5}'.format(cname[:5]) for cname in classname_list]

            # Prepend class name on each line and add to the top
            cm_str_lines = [' ' * 16 + ' '.join(classname_headers)]
            cm_str_lines += ['{:>15}'.format(cn[:15]) + ' ' + cm_line for cn, cm_line in zip(classname_list, cm_str.splitlines())]

            # Print formatted confusion matrix
            print("Confusion matrix: ")
            print(*cm_str_lines, sep='\n')

            # Plot confusion matrix
            
            # To manually add more space at bottom: plt.rcParams['figure.subplot.bottom'] = 0.1
            #
            # Add 0.5 to figsize for every class. For two classes, this will result in
            # fig = plt.figure(figsize=[4,4])
            fig = vis_utils.plot_confusion_matrix(
                            classifier_cm_array,
                            classname_list,
                            normalize=False,
                            title='Confusion matrix',
                            cmap=plt.cm.Blues,
                            vmax=1.0,
                            use_colorbar=True,
                            y_label=True)
            cm_figure_relative_filename = 'confusion_matrix.png'
            cm_figure_filename = os.path.join(output_dir, cm_figure_relative_filename)
            plt.savefig(cm_figure_filename)
            plt.close(fig)

        # ...if we have classification results
        
        
        ##%% Render output

        # Write p/r table to .csv file in output directory
        pr_table_filename = os.path.join(output_dir, 'prec_recall.csv')
        precisions_recalls.to_csv(pr_table_filename, index=False)

        # Write precision/recall plot to .png file in output directory
        t = 'Precision-Recall curve: AP={:0.1%}, P@{:0.1%}={:0.1%}'.format(
                average_precision, target_recall, precision_at_target_recall)
        fig = vis_utils.plot_precision_recall_curve(precisions, recalls, t)
        pr_figure_relative_filename = 'prec_recall.png'
        pr_figure_filename = os.path.join(output_dir, pr_figure_relative_filename)
        plt.savefig(pr_figure_filename)
        # plt.show(block=False)
        plt.close(fig)


        ##%% Sampling
        
        # Sample true/false positives/negatives with correct/incorrect top-1
        # classification and render to html

        # Accumulate html image structs (in the format expected by write_html_image_lists)
        # for each category, e.g. 'tp', 'fp', ..., 'class_bird', ...
        images_html = collections.defaultdict(lambda: [])
        # Add default entries by accessing them for the first time
        [images_html[res] for res in ['tp', 'tpc', 'tpi', 'fp', 'tn', 'fn']]  # Siyu: what does this do? This line should have no effect
        for res in images_html.keys():
            os.makedirs(os.path.join(output_dir, res), exist_ok=True)

        image_count = len(images_to_visualize)

        # Each element will be a list of 2-tuples, with elements [collection name,html info struct]
        rendering_results = []
        
        # Each element will be a three-tuple with elements file,max_conf,detections
        files_to_render = []
        
        # Assemble the information we need for rendering, so we can parallelize without
        # dealing with Pandas
        # i_row = 0; row = images_to_visualize.iloc[0]
        for _, row in images_to_visualize.iterrows():

            # Filenames should already have been normalized to either '/' or '\'
            files_to_render.append([row['file'],row['max_detection_conf'],row['detections']])
            
        def render_image_with_gt(file_info):

            image_relative_path = file_info[0]
            max_conf = file_info[1]
            detections = file_info[2]

            # This should already have been normalized to either '/' or '\'

            image_id = ground_truth_indexed_db.filename_to_id.get(image_relative_path, None)
            if image_id is None:
                print('Warning: couldn''t find ground truth for image {}'.format(image_relative_path))
                return None

            image = ground_truth_indexed_db.image_id_to_image[image_id]
            annotations = ground_truth_indexed_db.image_id_to_annotations[image_id]

            gt_status = image['_detection_status']

            gt_presence = bool(gt_status)

            gt_classes = CameraTrapJsonUtils.annotations_to_classnames(
                    annotations,ground_truth_indexed_db.cat_id_to_name)
            gt_class_summary = ','.join(gt_classes)

            if gt_status > DetectionStatus.DS_MAX_DEFINITIVE_VALUE:
                print('Skipping image {}, does not have a definitive ground truth status (status: {}, classes: {})'.format(
                        image_id, gt_status, gt_class_summary))
                return None

            detected = max_conf > options.confidence_threshold

            if gt_presence and detected:
                if '_classification_accuracy' not in image.keys():
                    res = 'tp'
                elif np.isclose(1, image['_classification_accuracy']):
                    res = 'tpc'
                else:
                    res = 'tpi'
            elif not gt_presence and detected:
                res = 'fp'
            elif gt_presence and not detected:
                res = 'fn'
            else:
                res = 'tn'

            display_name = '<b>Result type</b>: {}, <b>Presence</b>: {}, <b>Class</b>: {}, <b>Max conf</b>: {:0.2f}%, <b>Image</b>: {}'.format(
                res.upper(), str(gt_presence), gt_class_summary,
                max_conf * 100, image_relative_path)

            rendered_image_html_info = render_bounding_boxes(options.image_base_dir,
                                                                image_relative_path,
                                                                display_name,
                                                                detections,
                                                                res,
                                                                detection_categories_map,
                                                                classification_categories_map,
                                                                options)

            image_result = None
            if len(rendered_image_html_info) > 0:
                image_result = [[res,rendered_image_html_info]]
                for gt_class in gt_classes:
                    image_result.append(['class_{}'.format(gt_class),rendered_image_html_info])
            
            return image_result
            
        # ...def render_image_with_gt(file_info)
        
        start_time = time.time()
        if options.parallelize_rendering:
            if options.parallelize_rendering_n_cores is None:
                pool = ThreadPool()
            else:
                print('Rendering images with {} workers'.format(options.parallelize_rendering_n_cores))
                pool = ThreadPool(options.parallelize_rendering_n_cores)
            rendering_results = list(tqdm(pool.imap(render_image_with_gt, files_to_render), total=len(files_to_render)))    
        else:
            # file_info = files_to_render[0]
            for file_info in tqdm(files_to_render):        
                rendering_results.append(render_image_with_gt(file_info))
        elapsed = time.time() - start_time
        
        # Map all the rendering results in the list rendering_results into the 
        # dictionary images_html
        image_rendered_count = 0
        for rendering_result in rendering_results:
            if rendering_result is None:
                continue
            image_rendered_count += 1
            for assignment in rendering_result:
                images_html[assignment[0]].append(assignment[1])
                
        # Prepare the individual html image files
        image_counts = prepare_html_subpages(images_html, output_dir)

        print('{} images rendered (of {})'.format(image_rendered_count,image_count))

        # Write index.html
        all_tp_count = image_counts['tp'] + image_counts['tpc'] + image_counts['tpi']
        total_count = all_tp_count + image_counts['tn'] + image_counts['fp'] + image_counts['fn']
        
        classification_detection_results = """&nbsp;&nbsp;&nbsp;&nbsp;<a href="tpc.html">with all correct top-1 predictions (TPC)</a> ({})<br/>
           &nbsp;&nbsp;&nbsp;&nbsp;<a href="tpi.html">with one or more incorrect top-1 prediction (TPI)</a> ({})<br/>
           &nbsp;&nbsp;&nbsp;&nbsp;<a href="tp.html">without classification evaluation</a><sup>*</sup> ({})<br/>""".format(
            image_counts['tpc'],
            image_counts['tpi'],
            image_counts['tp']            
        )
        
        index_page = """<html>
        {}
        <body>
        <h2>Evaluation</h2>

        <h3>Sample images</h3>
        <div style="margin-left:20px;">
        <p>A sample of {} images, annotated with detections above {:.1%} confidence.</p>
        <a href="tp.html">True positives (TP)</a> ({}) ({:0.1%})<br/>
        CLASSIFICATION_PLACEHOLDER_1
        <a href="tn.html">True negatives (TN)</a> ({}) ({:0.1%})<br/>
        <a href="fp.html">False positives (FP)</a> ({}) ({:0.1%})<br/>
        <a href="fn.html">False negatives (FN)</a> ({}) ({:0.1%})<br/>
        CLASSIFICATION_PLACEHOLDER_2
        </div>        
        """.format(
            style_header,
            image_count, options.confidence_threshold,
            all_tp_count, all_tp_count/total_count,
            image_counts['tn'], image_counts['tn']/total_count,
            image_counts['fp'], image_counts['fp']/total_count,
            image_counts['fn'], image_counts['fn']/total_count
        )
        
        index_page += """
            <h3>Detection results</h3>
            <div class="contentdiv">
            <p>At a confidence threshold of {:0.1%}, precision={:0.1%}, recall={:0.1%}</p>
            <p><strong>Precision/recall summary for all {} images</strong></p><img src="{}"><br/>
            </div>
            """.format(
                options.confidence_threshold, precision_at_confidence_threshold, recall_at_confidence_threshold,
                len(detection_results), pr_figure_relative_filename
           )
        
        if len(classifier_accuracies) > 0:
            index_page = index_page.replace('CLASSIFICATION_PLACEHOLDER_1',classification_detection_results)
            index_page = index_page.replace('CLASSIFICATION_PLACEHOLDER_2',"""<p><sup>*</sup>We do not evaluate the classification result of images 
                if the classification information is missing, if the image contains
                categories like &lsquo;empty&rsquo; or &lsquo;human&rsquo;, or if the image has multiple 
                classification labels.</p>""")
        else:
            index_page = index_page.replace('CLASSIFICATION_PLACEHOLDER_1','')
            index_page = index_page.replace('CLASSIFICATION_PLACEHOLDER_2','')
            
        if len(classifier_accuracies) > 0:
            index_page += """
                <h3>Classification results</h3>
                <div class="contentdiv">
                <p>Classification accuracy: {:.2%}<br>
                The accuracy is computed only for images with exactly one classification label.
                The accuracy of an image is computed as 1/(number of unique detected top-1 classes),
                i.e. if the model detects multiple boxes with different top-1 classes, then the accuracy
                decreases and the image is put into 'TPI'.</p>
                <p>Confusion matrix:</p>
                <p><img src="{}"></p>
                <div style='font-family:monospace;display:block;'>{}</div>
                </div>
                """.format(
                    np.mean(classifier_accuracies),
                    cm_figure_relative_filename,
                    "<br>".join(cm_str_lines).replace(' ', '&nbsp;')
                )
                
        # Show links to each GT class
        #
        # We could do this without classification results; currently we don't.
        if len(classname_to_idx) > 0:
            
            index_page += '<h3>Images of specific classes</h3><br/><div class="contentdiv">'
            # Add links to all available classes
            for cname in sorted(classname_to_idx.keys()):
                index_page += "<a href='class_{0}.html'>{0}</a> ({1})<br>".format(
                    cname,
                    len(images_html['class_{}'.format(cname)]))
            index_page += "</div>"
            
        # Close body and html tags
        index_page += "</body></html>"
        output_html_file = os.path.join(output_dir, 'index.html')
        with open(output_html_file, 'w') as f:
            f.write(index_page)

        print('Finished writing html to {}'.format(output_html_file))

    # ...for each image
    
    
    ##%% Otherwise, if we don't have ground truth...

    else:

        ##%% Sample detections/non-detections

        # Accumulate html image structs (in the format expected by write_html_image_lists)
        # for each category
        images_html = collections.defaultdict(lambda: [])        
        
        # Add default entries by accessing them for the first time
        [images_html[res] for res in ['detections', 'non_detections']]
        if options.include_almost_detections:
            images_html['almost_detections']
            
        # Create output directories
        for res in images_html.keys():
            os.makedirs(os.path.join(output_dir, res), exist_ok=True)

        image_count = len(images_to_visualize)
        has_classification_info = False
        
        # Each element will be a list of 2-tuples, with elements [collection name,html info struct]
        rendering_results = []

        # Each element will be a three-tuple with elements file,max_conf,detections
        files_to_render = []
        
        # Assemble the information we need for rendering, so we can parallelize without
        # dealing with Pandas
        # i_row = 0; row = images_to_visualize.iloc[0]
        for _, row in images_to_visualize.iterrows():

            # Filenames should already have been normalized to either '/' or '\'
            files_to_render.append([row['file'],row['max_detection_conf'],row['detections']])
            
        # Local function for parallelization
        def render_image_no_gt(file_info):
            
            image_relative_path = file_info[0]
            max_conf = file_info[1]
            detections = file_info[2]
            
            detection_status = DetectionStatus.DS_UNASSIGNED            
            if max_conf >= options.confidence_threshold:
                detection_status = DetectionStatus.DS_POSITIVE
            else:
                if options.include_almost_detections:
                    if max_conf >= options.almost_detection_confidence_threshold:
                        detection_status = DetectionStatus.DS_ALMOST
                    else:
                        detection_status = DetectionStatus.DS_NEGATIVE
                else:
                    detection_status = DetectionStatus.DS_NEGATIVE
            
            if detection_status == DetectionStatus.DS_POSITIVE:
                res = 'detections'
            elif detection_status == DetectionStatus.DS_NEGATIVE:
                res = 'non_detections'
            else:
                assert detection_status == DetectionStatus.DS_ALMOST
                res = 'almost_detections'

            display_name = '<b>Result type</b>: {}, <b>Image</b>: {}, <b>Max conf</b>: {}'.format(
                res, image_relative_path, max_conf)

            rendering_options = copy.copy(options)
            if detection_status == DetectionStatus.DS_ALMOST:
                rendering_options.confidence_threshold = rendering_options.almost_detection_confidence_threshold
            rendered_image_html_info = render_bounding_boxes(options.image_base_dir,
                                                                image_relative_path,
                                                                display_name,
                                                                detections,
                                                                res,
                                                                detection_categories_map,
                                                                classification_categories_map,
                                                                rendering_options)
            
            image_result = None
            if len(rendered_image_html_info) > 0:
                image_result = [[res,rendered_image_html_info]]
                for det in detections:
                    if 'classifications' in det:
                        top1_class = classification_categories_map[det['classifications'][0][0]]
                        image_result.append(['class_{}'.format(top1_class),rendered_image_html_info])
            
            return image_result
        
        # ...def render_image_no_gt(file_info):
        
        start_time = time.time()
        if options.parallelize_rendering:
            if options.parallelize_rendering_n_cores is None:
                pool = ThreadPool()
            else:
                print('Rendering images with {} workers'.format(options.parallelize_rendering_n_cores))
                pool = ThreadPool(options.parallelize_rendering_n_cores)
            rendering_results = list(tqdm(pool.imap(render_image_no_gt, files_to_render), total=len(files_to_render)))    
        else:
            for file_info in tqdm(files_to_render):        
                rendering_results.append(render_image_no_gt(file_info))            
        elapsed = time.time() - start_time
        
        # Map all the rendering results in the list rendering_results into the 
        # dictionary images_html
        image_rendered_count = 0
        for rendering_result in rendering_results:
            if rendering_result is None:
                continue
            image_rendered_count += 1
            for assignment in rendering_result:
                if 'class' in assignment[0]:
                    has_classification_info = True
                images_html[assignment[0]].append(assignment[1])
                
        # Prepare the individual html image files
        image_counts = prepare_html_subpages(images_html, output_dir)
        
        print('Rendered {} images (of {}) in {} ({} per image)'.format(image_rendered_count,
              image_count,humanfriendly.format_timespan(elapsed),
              humanfriendly.format_timespan(elapsed/image_rendered_count)))

        # Write index.HTML
        total_images = image_counts['detections'] + image_counts['non_detections']
        if options.include_almost_detections:
            total_images += image_counts['almost_detections']
        assert(total_images == image_count)
        
        almost_detection_string = ''
        if options.include_almost_detections:
            almost_detection_string = ' (&ldquo;almost detection&rdquo; threshold at {:.1%})'.format(options.almost_detection_confidence_threshold)
            
        index_page = """<html>{}<body>
        <h2>Visualization of results</h2>
        <p>A sample of {} images, annotated with detections above {:.1%} confidence{}.</p>
        <h3>Sample images</h3>
        <div class="contentdiv">
        <a href="detections.html">detections</a> ({}, {:.1%})<br/>
        <a href="non_detections.html">non-detections</a> ({}, {:.1%})<br/>""".format(
            style_header,image_count, options.confidence_threshold, almost_detection_string,
            image_counts['detections'], image_counts['detections']/total_images,
            image_counts['non_detections'], image_counts['non_detections']/total_images
        )
        
        if options.include_almost_detections:
            index_page += """<a href="almost_detections.html">almost-detections</a> ({}, {:.1%})<br/>""".format( 
                    image_counts['almost_detections'], image_counts['almost_detections']/total_images)
        
        index_page += '</div>\n'
        
        if has_classification_info:
            index_page += "<h3>Images of detected classes</h3>"
            index_page += "<p>The same image might appear under multiple classes if multiple species were detected.</p>\n<div class='contentdiv'>\n"
        
            # Add links to all available classes
            for cname in sorted(classification_categories_map.values()):
                ccount = len(images_html['class_{}'.format(cname)])
                if ccount > 0:
                    index_page += "<a href='class_{}.html'>{}</a> ({})<br/>\n".format(cname, cname.lower(), ccount)
            index_page += "</div>\n"
            
        index_page += "</body></html>"
        output_html_file = os.path.join(output_dir, 'index.html')
        with open(output_html_file, 'w') as f:
            f.write(index_page)

        print('Finished writing html to {}'.format(output_html_file))

        # os.startfile(output_html_file)
        
    # ...if we do/don't have ground truth

    ppresults.output_html_file = output_html_file
    return ppresults

# ...process_batch_results


#%% Interactive driver(s)

if False:

    #%%

    base_dir = r'D:\wildlife_data\bh'
    options = PostProcessingOptions()
    options.image_base_dir = base_dir
    options.output_dir = os.path.join(base_dir, 'postprocessing_filtered')
    options.api_output_filename_replacements = {} # {'20190430cameratraps\\':''}
    options.ground_truth_filename_replacements = {'\\data\\blob\\':''}
    options.api_output_file = os.path.join(base_dir, 'bh_5570_detections.filtered.csv')
    options.ground_truth_json_file = os.path.join(base_dir, 'bh.json')
    options.unlabeled_classes = ['human']

    ppresults = process_batch_results(options)
    # os.start(ppresults.output_html_file)


#%% Command-line driver

def main():

    default_options = PostProcessingOptions()

    parser = argparse.ArgumentParser()
    parser.add_argument('api_output_file', action='store', type=str,
                        help='.json file produced by the batch inference API (detection/classification, required)')
    parser.add_argument('output_dir', action='store', type=str,
                        help='Base directory for output (required)')
    parser.add_argument('--image_base_dir', action='store', type=str,
                        help='Base directory for images (optional, can compute statistics without images)')
    parser.add_argument('--ground_truth_json_file', action='store', type=str,
                        help='Ground truth labels (optional, can render detections without ground truth)')

    parser.add_argument('--confidence_threshold', action='store', type=float,
                        default=default_options.confidence_threshold,
                        help='Confidence threshold for statistics and visualization')
    parser.add_argument('--target_recall', action='store', type=float, default=default_options.target_recall,
                        help='Target recall (for statistics only)')
    parser.add_argument('--num_images_to_sample', action='store', type=int,
                        default=default_options.num_images_to_sample,
                        help='Number of images to visualize (defaults to 500) (-1 to include all images)')
    parser.add_argument('--viz_target_width', action='store', type=int, default=default_options.viz_target_width,
                        help='Output image width')
    parser.add_argument('--random_output_sort', action='store_true', help='Sort output randomly (defaults to sorting by filename)')

    if len(sys.argv[1:]) == 0:
        parser.print_help()
        parser.exit()

    args = parser.parse_args()
    args.sort_html_by_filename = not args.random_output_sort

    options = PostProcessingOptions()
    args_to_object(args,options)

    process_batch_results(options)


if __name__ == '__main__':

    main()
