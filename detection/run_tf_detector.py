######
#
# run_tf_detector.py
#
# Functions to load a TensorFlow detection model, run inference,
# render bounding boxes on images, and write out the resulting
# images (with bounding boxes).
#
# This script depends on nothign else in our repo, just standard pip
# installs.  It's a good way to test our detector on a handful of images and
# get super-satisfying, graphical results.  It's also a good way to see how
# fast a detector model will run on a particular machine.
#
# This script is not a good way to process lots and lots of images; it loads all
# the images first, then runs the model.  If you want to run a detector (e.g. ours)
# on lots of images, you should check out:
#
# 1) run_tf_detector_batch.py (for local execution)
# 
# 2) https://github.com/microsoft/CameraTraps/tree/master/api/batch_processing
#    (for running large jobs on Azure ML)
#
# See the "test driver" cell for example invocation.
#
# If no output directory is specified, writes detections for c:\foo\bar.jpg to
# c:\foo\bar_detections.jpg .
#
######

#%% Constants, imports, environment

import argparse
import glob
import os
import sys
import time

import PIL
import humanfriendly
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.image as mpimg
import matplotlib.patches as patches
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import tensorflow as tf
from tqdm import tqdm

DEFAULT_CONFIDENCE_THRESHOLD = 0.85

# Stick this into filenames before the extension for the rendered result
DETECTION_FILENAME_INSERT = '_detections'

BOX_COLORS = ['b','g','r']
DEFAULT_LINE_WIDTH = 10
SHOW_CONFIDENCE_VALUES = False

# Suppress excessive tensorflow output
tf.logging.set_verbosity(tf.logging.ERROR)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


#%% Core detection functions

def load_model(checkpoint):
    """
    Load a detection model (i.e., create a graph) from a .pb file
    """

    detection_graph = tf.Graph()
    with detection_graph.as_default():
        od_graph_def = tf.GraphDef()
        with tf.gfile.GFile(checkpoint, 'rb') as fid:
            serialized_graph = fid.read()
            od_graph_def.ParseFromString(serialized_graph)
            tf.import_graph_def(od_graph_def, name='')
    
    return detection_graph


def generate_detections(detection_graph,images):
    """
    boxes,scores,classes,images = generate_detections(detection_graph,images)

    Run an already-loaded detector network on a set of images.

    [images] can be a list of numpy arrays or a list of filenames.  Non-list inputs will be
    wrapped into a list.

    Boxes are returned in relative coordinates as (top, left, bottom, right); 
    x,y origin is the upper-left.
    
    [boxes] will be returned as a numpy array of size nImages x nDetections x 4.
    
    [scores] and [classes] will each be returned as a numpy array of size nImages x nDetections.
    
    [images] is a set of numpy arrays corresponding to the input parameter [images], which may have
    have been either arrays or filenames.    
    """

    if not isinstance(images,list):
        images = [images]
    else:
        images = images.copy()

    print('Loading images...')
    startTime = time.time()
    
    # Load images if they're not already numpy arrays
    # iImage = 0; image = images[iImage]
    for iImage,image in enumerate(tqdm(images)):
        if isinstance(image,str):
            
            # Load the image as an nparray of size h,w,nChannels
            
            # There was a time when I was loading with PIL and switched to mpimg,
            # but I can't remember why, and converting to RGB is a very good reason
            # to load with PIL, since mpimg doesn't give any indication of color 
            # order, which basically breaks all .png files.
            #
            # So if you find a bug related to using PIL, update this comment
            # to indicate what it was, but also disable .png support.
            image = PIL.Image.open(image).convert("RGB"); image = np.array(image)
            # image = mpimg.imread(image)
            
            # This shouldn't be necessary when loading with PIL and converting to RGB
            nChannels = image.shape[2]
            if nChannels > 3:
                print('Warning: trimming channels from image')
                image = image[:,:,0:3]
            images[iImage] = image
        else:
            assert isinstance(image,np.ndarray)

    elapsed = time.time() - startTime
    print("Finished loading {} file(s) in {}".format(len(images),
          humanfriendly.format_timespan(elapsed)))    
    
    boxes = []
    scores = []
    classes = []
    
    nImages = len(images)

    print('Running detector...')    
    startTime = time.time()
    firstImageCompleteTime = None
    
    with detection_graph.as_default():
        
        with tf.Session(graph=detection_graph) as sess:
            
            for iImage,imageNP in tqdm(enumerate(images)): 
                
                imageNP_expanded = np.expand_dims(imageNP, axis=0)
                image_tensor = detection_graph.get_tensor_by_name('image_tensor:0')
                box = detection_graph.get_tensor_by_name('detection_boxes:0')
                score = detection_graph.get_tensor_by_name('detection_scores:0')
                clss = detection_graph.get_tensor_by_name('detection_classes:0')
                num_detections = detection_graph.get_tensor_by_name('num_detections:0')
                
                # Actual detection
                (box, score, clss, num_detections) = sess.run(
                        [box, score, clss, num_detections],
                        feed_dict={image_tensor: imageNP_expanded})

                boxes.append(box)
                scores.append(score)
                classes.append(clss)
            
                if iImage == 0:
                    firstImageCompleteTime = time.time()
                    
            # ...for each image                
    
        # ...with tf.Session

    # ...with detection_graph.as_default()
    
    elapsed = time.time() - startTime
    if nImages == 1:
        print("Finished running detector in {}".format(humanfriendly.format_timespan(elapsed)))
    else:
        firstImageElapsed = firstImageCompleteTime - startTime
        remainingImagesElapsed = elapsed - firstImageElapsed
        remainingImagesTimePerImage = remainingImagesElapsed/(nImages-1)
        
        print("Finished running detector on {} images in {} ({} for the first image, {} for each subsequent image)".format(len(images),
              humanfriendly.format_timespan(elapsed),
              humanfriendly.format_timespan(firstImageElapsed),
              humanfriendly.format_timespan(remainingImagesTimePerImage)))
    
    nBoxes = len(boxes)
    
    # Currently "boxes" is a list of length nImages, where each element is shaped as
    #
    # 1,nDetections,4
    #
    # This implicitly banks on TF giving us back a fixed number of boxes, let's assert on this
    # to make sure this doesn't silently break in the future.
    nDetections = -1
    # iBox = 0; box = boxes[iBox]
    for iBox,box in enumerate(boxes):
        nDetectionsThisBox = box.shape[1]
        assert (nDetections == -1 or nDetectionsThisBox == nDetections), 'Detection count mismatch'
        nDetections = nDetectionsThisBox
        assert(box.shape[0] == 1)
    
    # "scores" is a length-nImages list of elements with size 1,nDetections
    assert(len(scores) == nImages)
    for(iScore,score) in enumerate(scores):
        assert score.shape[0] == 1
        assert score.shape[1] == nDetections
        
    # "classes" is a length-nImages list of elements with size 1,nDetections
    #
    # Still as floats, but really representing ints
    assert(len(classes) == nBoxes)
    for(iClass,c) in enumerate(classes):
        assert c.shape[0] == 1
        assert c.shape[1] == nDetections
            
    # Squeeze out the empty axis
    boxes = np.squeeze(np.array(boxes),axis=1)
    scores = np.squeeze(np.array(scores),axis=1)
    classes = np.squeeze(np.array(classes),axis=1).astype(int)
    
    # boxes is nImages x nDetections x 4
    assert(len(boxes.shape) == 3)
    assert(boxes.shape[0] == nImages)
    assert(boxes.shape[1] == nDetections)
    assert(boxes.shape[2] == 4)
    
    # scores and classes are both nImages x nDetections
    assert(len(scores.shape) == 2)
    assert(scores.shape[0] == nImages)
    assert(scores.shape[1] == nDetections)
    
    assert(len(classes.shape) == 2)
    assert(classes.shape[0] == nImages)
    assert(classes.shape[1] == nDetections)
    
    return boxes,scores,classes,images


#%% Rendering functions

def render_bounding_box(box, score, classLabel, inputFileName, outputFileName=None,
                          confidenceThreshold=DEFAULT_CONFIDENCE_THRESHOLD,linewidth=DEFAULT_LINE_WIDTH):
    """
    Convenience wrapper to apply render_bounding_boxes to a single image
    """
    outputFileNames = []
    if outputFileName is not None:
        outputFileNames = [outputFileName]
    scores = [[score]]
    boxes = [[box]]
    render_bounding_boxes(boxes,scores,[classLabel],[inputFileName],outputFileNames,
                          confidenceThreshold,linewidth)


def render_bounding_boxes(boxes, scores, classes, inputFileNames, outputFileNames=[],
                          confidenceThreshold=DEFAULT_CONFIDENCE_THRESHOLD, linewidth=DEFAULT_LINE_WIDTH):
    """
    Render bounding boxes on the image files specified in [inputFileNames].  
    
    [boxes] and [scores] should be in the format returned by generate_detections, 
    specifically [top, left, bottom, right] in normalized units, where the
    origin is the upper-left.    
    
    "classes" is currently unused, it's a placeholder for adding text annotations
    later.
    """

    nImages = len(inputFileNames)
    iImage = 0

    for iImage in range(0,nImages):

        inputFileName = inputFileNames[iImage]

        if iImage >= len(outputFileNames):
            outputFileName = ''
        else:
            outputFileName = outputFileNames[iImage]

        if len(outputFileName) == 0:
            name, ext = os.path.splitext(inputFileName)
            outputFileName = "{}{}{}".format(name,DETECTION_FILENAME_INSERT,ext)

        image = mpimg.imread(inputFileName)
        iBox = 0; box = boxes[iImage][iBox]
        dpi = 100
        s = image.shape; imageHeight = s[0]; imageWidth = s[1]
        figsize = imageWidth / float(dpi), imageHeight / float(dpi)

        plt.figure(figsize=figsize)
        ax = plt.axes([0,0,1,1])
        
        # Display the image
        ax.imshow(image)
        ax.set_axis_off()
    
        # plt.show()
        for iBox,box in enumerate(boxes[iImage]):

            score = scores[iImage][iBox]
            if score < confidenceThreshold:
                continue

            # top, left, bottom, right 
            #
            # x,y origin is the upper-left
            topRel = box[0]
            leftRel = box[1]
            bottomRel = box[2]
            rightRel = box[3]
            
            x = leftRel * imageWidth
            y = topRel * imageHeight
            w = (rightRel-leftRel) * imageWidth
            h = (bottomRel-topRel) * imageHeight
            
            # Location is the bottom-left of the rect
            #
            # Origin is the upper-left
            iLeft = x
            iBottom = y
            iClass = int(classes[iImage][iBox])
            
            boxColor = BOX_COLORS[iClass % len(BOX_COLORS)]
            rect = patches.Rectangle((iLeft,iBottom),w,h,linewidth=linewidth,edgecolor=boxColor,
                                     facecolor='none')
            
            # Add the patch to the Axes
            ax.add_patch(rect)        
            
            if SHOW_CONFIDENCE_VALUES:
                pLabel = 'Class {} ({:.2f})'.format(iClass,score)
                ax.text(iLeft+5,iBottom+5,pLabel,color=boxColor,fontsize=12,
                        verticalalignment='top',bbox=dict(facecolor='black'))
            
        # ...for each box

        # This is magic goop that removes whitespace around image plots (sort of)        
        plt.subplots_adjust(top = 1, bottom = 0, right = 1, left = 0, hspace = 0, 
                            wspace = 0)
        plt.margins(0,0)
        ax.xaxis.set_major_locator(ticker.NullLocator())
        ax.yaxis.set_major_locator(ticker.NullLocator())
        ax.axis('tight')
        ax.set(xlim=[0,imageWidth],ylim=[imageHeight,0],aspect=1)
        plt.axis('off')                

        # plt.savefig(outputFileName, bbox_inches='tight', pad_inches=0.0, dpi=dpi, transparent=True)
        plt.savefig(outputFileName, dpi=dpi, transparent=True, optimize=True, quality=90)
        plt.close()
        # os.startfile(outputFileName)

    # ...for each image

# ...def render_bounding_boxes


def load_and_run_detector(modelFile, imageFileNames, outputDir=None,
                          confidenceThreshold=DEFAULT_CONFIDENCE_THRESHOLD, detection_graph=None):
    
    if len(imageFileNames) == 0:        
        print('Warning: no files available')
        return
        
    # Load and run detector on target images
    print('Loading model...')
    startTime = time.time()
    if detection_graph is None:
        detection_graph = load_model(modelFile)
    elapsed = time.time() - startTime
    print("Loaded model in {}".format(humanfriendly.format_timespan(elapsed)))
    
    boxes,scores,classes,images = generate_detections(detection_graph,imageFileNames)
    
    assert len(boxes) == len(imageFileNames)
    
    print('Rendering output...')
    startTime = time.time()
    
    outputFullPaths = []
    outputFileNames = {}
    
    if outputDir is not None:
            
        os.makedirs(outputDir,exist_ok=True)
        
        for iFn,fullInputPath in enumerate(tqdm(imageFileNames)):
            
            fn = os.path.basename(fullInputPath).lower()            
            name, ext = os.path.splitext(fn)
            fn = "{}{}{}".format(name,DETECTION_FILENAME_INSERT,ext)
            
            # Since we'll be writing a bunch of files to the same folder, rename
            # as necessary to avoid collisions
            if fn in outputFileNames:
                nCollisions = outputFileNames[fn]
                fn = str(nCollisions) + '_' + fn
                outputFileNames[fn] = nCollisions + 1
            else:
                outputFileNames[fn] = 0
            outputFullPaths.append(os.path.join(outputDir,fn))
    
        # ...for each file
        
    # ...if we're writing files to a directory other than the input directory
    
    plt.ioff()
    
    render_bounding_boxes(boxes=boxes, scores=scores, classes=classes, 
                          inputFileNames=imageFileNames, outputFileNames=outputFullPaths,
                          confidenceThreshold=confidenceThreshold)
    
    elapsed = time.time() - startTime
    print("Rendered output in {}".format(humanfriendly.format_timespan(elapsed)))
    
    return detection_graph


#%% Interactive driver

if False:
    
    #%%
    
    detection_graph = None
    
    #%%
    
    modelFile = r'D:\temp\models\megadetector_v3.pb'
    imageDir = r'D:\temp\demo_images\ssmini'    
    imageFileNames = [fn for fn in glob.glob(os.path.join(imageDir,'*.jpg'))
         if (not 'detections' in fn)]
    # imageFileNames = [r"D:\temp\test\1\NE2881-9_RCNX0195_xparent.png"]
    
    detection_graph = load_and_run_detector(modelFile,imageFileNames,
                                            confidenceThreshold=DEFAULT_CONFIDENCE_THRESHOLD,
                                            detection_graph=detection_graph)
    

#%% File helper functions

imageExtensions = ['.jpg','.jpeg','.gif','.png']
    
def isImageFile(s):
    """
    Check a file's extension against a hard-coded set of image file extensions    '
    """
    ext = os.path.splitext(s)[1]
    return ext.lower() in imageExtensions
    
    
def findImageStrings(strings):
    """
    Given a list of strings that are potentially image file names, look for strings
    that actually look like image file names (based on extension).
    """
    imageStrings = []
    bIsImage = [False] * len(strings)
    for iString,f in enumerate(strings):
        bIsImage[iString] = isImageFile(f) 
        if bIsImage[iString]:
            imageStrings.append(f)
        
    return imageStrings

    
def findImages(dirName,bRecursive=False):
    """
    Find all files in a directory that look like image file names
    """
    if bRecursive:
        strings = glob.glob(os.path.join(dirName,'**','*.*'), recursive=True)
    else:
        strings = glob.glob(os.path.join(dirName,'*.*'))
        
    imageStrings = findImageStrings(strings)
    
    return imageStrings

    
#%% Command-line driver
    
def main():
    
    # python run_tf_detector.py "D:\temp\models\object_detection\megadetector\megadetector_v2.pb" --imageFile "D:\temp\demo_images\test\S1_J08_R1_PICT0120.JPG"
    # python run_tf_detector.py "D:\temp\models\object_detection\megadetector\megadetector_v2.pb" --imageDir "d:\temp\demo_images\test"
    # python run_tf_detector.py "d:\temp\models\object_detection\megadetector\megadetector_v3.pb" --imageDir "d:\temp\test\in" --outputDir "d:\temp\test\out"
    
    parser = argparse.ArgumentParser()
    parser.add_argument('detectorFile', type=str)
    parser.add_argument('--imageDir', action='store', type=str, default='', 
                        help='Directory to search for images, with optional recursion')
    parser.add_argument('--imageFile', action='store', type=str, default='', 
                        help='Single file to process, mutually exclusive with imageDir')
    parser.add_argument('--threshold', action='store', type=float, 
                        default=DEFAULT_CONFIDENCE_THRESHOLD, 
                        help="Confidence threshold, don't render boxes below this confidence")
    parser.add_argument('--recursive', action='store_true', 
                        help='Recurse into directories, only meaningful if using --imageDir')
    parser.add_argument('--forceCpu', action='store_true', 
                        help='Force CPU detection, even if a GPU is available')
    parser.add_argument('--outputDir', type=str, default=None, 
                       help='Directory for output images (defaults to same as input)')
    
    if len(sys.argv[1:])==0:
        parser.print_help()
        parser.exit()
    
    args = parser.parse_args()    
    
    if len(args.imageFile) > 0 and len(args.imageDir) > 0:
        raise Exception('Cannot specify both image file and image dir')
    elif len(args.imageFile) == 0 and len(args.imageDir) == 0:
        raise Exception('Must specify either an image file or an image directory')
        
    if len(args.imageFile) > 0:
        imageFileNames = [args.imageFile]
    else:
        imageFileNames = findImages(args.imageDir,args.recursive)

    if args.forceCpu:
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'

    # Hack to avoid running on already-detected images
    imageFileNames = [x for x in imageFileNames if DETECTION_FILENAME_INSERT not in x]
                
    print('Running detector on {} images'.format(len(imageFileNames)))    
    
    load_and_run_detector(modelFile=args.detectorFile, imageFileNames=imageFileNames, 
                          confidenceThreshold=args.threshold, outputDir=args.outputDir)
    

if __name__ == '__main__':
    
    main()
