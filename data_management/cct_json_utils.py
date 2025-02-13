#######
#
# cct_json_utils.py
#
# Utilities for working with COCO Camera Traps .json databases
#
# Format spec:
#
# https://github.com/Microsoft/CameraTraps/blob/master/data_management/README.md#coco-cameratraps-format
#
#######


#%% Constants and imports

import os
import json
from collections import defaultdict


#%% Classes

class CameraTrapJsonUtils:
    """
    Miscellaneous utility functions for working with COCO Camera Traps databases
    """
    @staticmethod
    def annotations_to_string(annotations, cat_id_to_name):
        """
        Given a list of annotations and a mapping from class IDs to names, produces
        a concatenated class list, always sorting alphabetically.
        """
        class_names = CameraTrapJsonUtils.annotationsToClassnames(annotations, cat_id_to_name)
        return ','.join(class_names)

    @staticmethod
    def annotations_to_classnames(annotations, cat_id_to_name):
        """
        Given a list of annotations and a mapping from class IDs to names, produces
        a list of class names, always sorting alphabetically.
        """
        # Collect all names
        class_names = [cat_id_to_name[ann['category_id']] for ann in annotations]
        # Make names unique and sort
        class_names = sorted(list(set(class_names)))
        return class_names


class IndexedJsonDb:
    """
    Wrapper for a COCO Camera Traps database.

    Handles boilerplate dictionary creation that we do almost every time we load
    a .json database.
    """

    # The underlying .json db
    db = None

    # Useful dictionaries
    cat_id_to_name = None
    cat_name_to_id = None
    filename_to_id = None
    image_id_to_annotations = None


    def get_annotations_for_image(self, image):
        """
        Returns a list of annotations associated with [image]
        
        Returns None is the db has not been loaded, [] if no annotations are available
        """
        
        if self.db is None:
            return None
    
        if image['id'] not in self.image_id_to_annotations:
            return []
        
        image_annotations = self.image_id_to_annotations[image['id']]
        return image_annotations
    
    
    def get_classes_for_image(self, image):
        """
        Returns a list of class names associated with [image]
        
        Returns None is the db has not been loaded, [] if no annotations are available
        """
        
        if self.db is None:
            return None
    
        if image['id'] not in self.image_id_to_annotations:
            return []
        
        class_ids = []
        image_annotations = self.image_id_to_annotations[image['id']]
        for ann in image_annotations:
            class_ids.append(ann['category_id'])
        class_ids = list(set(class_ids))
        class_ids.sort()
        class_names = [self.cat_id_to_name[x] for x in class_ids]
        
        return class_names
        
        
    def __init__(self, json_filename, b_normalize_paths=False, filename_replacements={}):
        '''
        json_filename can also be an existing json db
        '''
        
        if isinstance(json_filename,str):
            self.db = json.load(open(json_filename))
        else:
            self.db = json_filename
    
        assert 'images' in self.db, 'Could not find image list in file {}, are you sure this is a COCO camera traps file?'.format(json_filename)
        
        if b_normalize_paths:
            # Normalize paths to simplify comparisons later
            for im in self.db['images']:
                im['file_name'] = os.path.normpath(im['file_name'])

        for s in filename_replacements:
            r = filename_replacements[s]
            for im in self.db['images']:
                im['file_name'] = im['file_name'].replace(s, r)
        
        ### Build useful mappings to facilitate working with the DB

        # Category ID <--> name
        self.cat_id_to_name = {cat['id']: cat['name'] for cat in self.db['categories']}
        self.cat_name_to_id = {cat['name']: cat['id'] for cat in self.db['categories']}

        # Image filename --> ID
        self.filename_to_id = {im['file_name']: im['id'] for im in self.db['images']}

        # Each image can potentially multiple annotations, hence using lists
        self.image_id_to_annotations = defaultdict(list)

        # Image ID --> image object
        self.image_id_to_image = {im['id']: im for im in self.db['images']}
        
        # Image ID --> annotations
        for ann in self.db['annotations']:
            self.image_id_to_annotations[ann['image_id']].append(ann)

    # ...__init__

# ...class IndexedJsonDb
