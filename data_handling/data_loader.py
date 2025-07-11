"""
Data Loading System for Surfing Pose Estimation Evaluation

This module handles loading video clips and their corresponding annotations
from the surfing dataset, supporting both H264 and FFV1 video formats.
"""

import json
import os
import random
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Union
import logging
from collections import defaultdict, Counter

import cv2
import numpy as np
import pandas as pd
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class VideoClip:
    """Represents a video clip with metadata and annotations."""

    file_path: str
    video_id: str
    camera: str  # SONY_300, SONY_70, etc.
    session: str  # SESSION_060325, etc.
    duration: float
    fps: float
    width: int
    height: int
    format: str  # h264 or ffv1
    zoom_level: str  # "default", "wide", "full"
    base_clip_id: str  # Base clip without zoom suffix
    annotations: List[Dict] = field(default_factory=list)

    @property
    def total_frames(self) -> int:
        return int(self.duration * self.fps)

    def get_frame_annotations(self, frame_idx: int) -> List[Dict]:
        """Get annotations for a specific frame based on timestamp."""
        timestamp = frame_idx / self.fps
        frame_annotations = []

        for annotation in self.annotations:
            if annotation["start"] <= timestamp <= annotation["end"]:
                frame_annotations.append(
                    {
                        "timestamp": timestamp,
                        "frame_idx": frame_idx,
                        "labels": annotation["labels"],
                        "maneuver_progress": (timestamp - annotation["start"])
                        / (annotation["end"] - annotation["start"]),
                    }
                )

        return frame_annotations


@dataclass
class DataSplit:
    """Represents train/validation/test splits of the dataset."""

    train: List[VideoClip] = field(default_factory=list)
    val: List[VideoClip] = field(default_factory=list)
    test: List[VideoClip] = field(default_factory=list)

    def get_split_stats(self) -> Dict:
        """Get statistics about the data splits including zoom distribution."""

        def get_stats(clips):
            zoom_counts = Counter(clip.zoom_level for clip in clips)
            return {
                "num_clips": len(clips),
                "total_duration": sum(clip.duration for clip in clips),
                "total_frames": sum(clip.total_frames for clip in clips),
                "zoom_distribution": dict(zoom_counts),
                "total_maneuvers": sum(len(clip.annotations) for clip in clips),
            }

        return {
            "train": get_stats(self.train),
            "val": get_stats(self.val),
            "test": get_stats(self.test),
        }


class SurfingDataLoader:
    """
    Main data loader for the surfing pose estimation dataset.

    Handles loading video clips and annotations, creating train/val/test splits,
    and providing data for pose estimation evaluation with zoom-aware processing.
    """

    def __init__(self, config: Dict):
        """
        Initialize the data loader with configuration.

        Args:
            config: Configuration dictionary containing dataset paths and settings
        """
        self.config = config
        self.dataset_config = config["dataset"]
        self.base_path = Path(self.dataset_config["base_data_path"])

        # Video and annotation paths
        self.video_clips_config = self.dataset_config["video_clips"]
        self.annotations_config = self.dataset_config["annotations"]

        # Data splits configuration
        self.splits_config = self.dataset_config["splits"]

        # Zoom handling configuration
        self.zoom_config = self.splits_config.get(
            "zoom_handling",
            {
                "enabled": True,
                "balanced_distribution": True,
                "target_distribution": {"default": 0.33, "wide": 0.33, "full": 0.34},
            },
        )

        # Initialize containers
        self.all_clips: List[VideoClip] = []
        self.annotations_data: Dict = {}
        self.data_splits: Optional[DataSplit] = None
        self.zoom_groups: Dict[str, List[VideoClip]] = defaultdict(list)

        logger.info(f"Initialized SurfingDataLoader with base path: {self.base_path}")
        if self.zoom_config.get("enabled", True):
            logger.info("Zoom-aware processing enabled with balanced distribution")

    def load_annotations(self) -> Dict:
        """
        Load all annotation files from the label studio exports.

        Returns:
            Dictionary mapping video files to their annotations
        """
        annotations = {}
        labels_path = self.base_path / self.annotations_config["labels_path"]

        # Load SONY_300 annotations
        sony_300_path = labels_path / self.annotations_config["sony_300_labels"]
        if sony_300_path.exists():
            for json_file in sony_300_path.glob("*.json"):
                logger.info(f"Loading annotations from {json_file}")
                with open(json_file, "r") as f:
                    data = json.load(f)
                    for item in data:
                        video_url = item["video_url"]
                        # Extract video file path from URL
                        video_file = self._extract_video_path_from_url(video_url)
                        if video_file:
                            annotations[video_file] = item["tricks"]

        # Load SONY_70 annotations
        sony_70_path = labels_path / self.annotations_config["sony_70_labels"]
        if sony_70_path.exists():
            for json_file in sony_70_path.glob("*.json"):
                logger.info(f"Loading annotations from {json_file}")
                with open(json_file, "r") as f:
                    data = json.load(f)
                    for item in data:
                        video_url = item["video_url"]
                        video_file = self._extract_video_path_from_url(video_url)
                        if video_file:
                            annotations[video_file] = item["tricks"]

        self.annotations_data = annotations
        logger.info(f"Loaded annotations for {len(annotations)} video files")
        return annotations

    def _extract_video_path_from_url(self, video_url: str) -> Optional[str]:
        """
        Extract video file path from Label Studio URL format.

        Args:
            video_url: URL from Label Studio export

        Returns:
            Relative path to video file or None if invalid
        """
        # URL format: /data/local-files/?d=SD_02_SURF_FOOTAGE_PREPT/03_CLIPPED/h264/SONY_300/SESSION_060325/C0019_clip_1.mp4
        if "?d=" in video_url:
            path_part = video_url.split("?d=")[1]
            # Remove the base data path prefix to get relative path
            if path_part.startswith("SD_02_SURF_FOOTAGE_PREPT/"):
                return path_part.replace("SD_02_SURF_FOOTAGE_PREPT/", "")
        return None

    def _extract_zoom_info(self, video_path: Path) -> Tuple[str, str]:
        """
        Extract zoom level and base clip ID from video filename.

        Args:
            video_path: Path to video file

        Returns:
            Tuple of (zoom_level, base_clip_id)
        """
        stem = video_path.stem

        if stem.endswith("_full"):
            return "full", stem[:-5]  # Remove '_full'
        elif stem.endswith("_wide"):
            return "wide", stem[:-5]  # Remove '_wide'
        else:
            return "default", stem

    def discover_video_clips(self, video_format: str = "h264") -> List[VideoClip]:
        """
        Discover all video clips in the specified format, grouping by zoom variations.

        Args:
            video_format: Video format to load ("h264" or "ffv1")

        Returns:
            List of VideoClip objects with balanced zoom distribution
        """
        if video_format == "h264":
            clips_path = self.base_path / self.video_clips_config["h264_path"]
        elif video_format == "ffv1":
            clips_path = self.base_path / self.video_clips_config["ffv1_path"]
        else:
            raise ValueError(f"Unsupported video format: {video_format}")

        if not clips_path.exists():
            logger.warning(f"Video clips path does not exist: {clips_path}")
            return []

        # First pass: Group all video files by base clip ID
        zoom_groups = defaultdict(lambda: defaultdict(list))

        # Discover all video files and group by zoom variations
        for camera_dir in clips_path.iterdir():
            if not camera_dir.is_dir():
                continue

            camera_name = camera_dir.name
            logger.info(f"Discovering clips for camera: {camera_name}")

            for session_dir in camera_dir.iterdir():
                if not session_dir.is_dir():
                    continue

                session_name = session_dir.name

                for video_file in session_dir.glob("*.mp4"):
                    zoom_level, base_clip_id = self._extract_zoom_info(video_file)
                    group_key = f"{camera_name}_{session_name}_{base_clip_id}"
                    zoom_groups[group_key][zoom_level].append(video_file)

        # Second pass: Select one zoom variation per base clip with balanced distribution
        selected_clips = []
        zoom_counters = Counter()
        target_dist = self.zoom_config.get(
            "target_distribution", {"default": 0.33, "wide": 0.33, "full": 0.34}
        )

        # Convert to list for shuffling
        group_items = list(zoom_groups.items())
        random.shuffle(group_items)

        for group_key, zoom_variants in group_items:
            # Determine which zoom level to use for this clip
            selected_zoom = self._select_balanced_zoom(
                zoom_variants, zoom_counters, target_dist
            )

            if selected_zoom and selected_zoom in zoom_variants:
                video_files = zoom_variants[selected_zoom]

                # Check which files have annotations
                annotated_files = []
                for video_file in video_files:
                    relative_path = str(video_file.relative_to(self.base_path))
                    if relative_path in self.annotations_data:
                        annotated_files.append(video_file)

                # Use annotated file if available, otherwise skip
                if annotated_files:
                    selected_file = annotated_files[0]  # Use first annotated file

                    try:
                        # Extract camera and session from path
                        path_parts = selected_file.relative_to(clips_path).parts
                        camera = path_parts[0]
                        session = path_parts[1]

                        clip = self._create_video_clip(
                            selected_file, camera, session, video_format, selected_zoom
                        )
                        if clip and clip.annotations:  # Only add clips with annotations
                            selected_clips.append(clip)
                            zoom_counters[selected_zoom] += 1

                    except Exception as e:
                        logger.warning(f"Failed to process {selected_file}: {e}")

        self.all_clips = selected_clips

        # Log zoom distribution statistics
        total_clips = len(selected_clips)
        if total_clips > 0:
            zoom_stats = {
                zoom: f"{count} ({count/total_clips:.1%})"
                for zoom, count in zoom_counters.items()
            }
            logger.info(
                f"Discovered {total_clips} clips with zoom distribution: {zoom_stats}"
            )

        return selected_clips

    def _select_balanced_zoom(
        self,
        zoom_variants: Dict[str, List],
        current_counters: Counter,
        target_distribution: Dict[str, float],
    ) -> Optional[str]:
        """
        Select zoom level to maintain balanced distribution.

        Args:
            zoom_variants: Available zoom variants for this clip
            current_counters: Current counts of each zoom level
            target_distribution: Target distribution percentages

        Returns:
            Selected zoom level or None if no variants available
        """
        available_zooms = list(zoom_variants.keys())
        if not available_zooms:
            return None

        total_clips = sum(current_counters.values())

        if total_clips == 0:
            # First clip - random selection
            return random.choice(available_zooms)

        # Calculate current distribution vs target
        zoom_priorities = []
        for zoom in available_zooms:
            current_ratio = current_counters[zoom] / total_clips
            target_ratio = target_distribution.get(zoom, 0.33)
            deficit = target_ratio - current_ratio
            zoom_priorities.append((deficit, zoom))

        # Sort by deficit (highest first) and select
        zoom_priorities.sort(reverse=True, key=lambda x: x[0])

        # Add some randomness while favoring underrepresented zooms
        if (
            len(zoom_priorities) > 1
            and zoom_priorities[0][0] - zoom_priorities[1][0] < 0.05
        ):
            # If deficit difference is small, randomly choose from top 2
            return random.choice([zoom_priorities[0][1], zoom_priorities[1][1]])
        else:
            return zoom_priorities[0][1]

    def _create_video_clip(
        self, video_path: Path, camera: str, session: str, format: str, zoom_level: str
    ) -> Optional[VideoClip]:
        """
        Create a VideoClip object from a video file.

        Args:
            video_path: Path to video file
            camera: Camera name (SONY_300, SONY_70, etc.)
            session: Session name (SESSION_060325, etc.)
            format: Video format (h264, ffv1)
            zoom_level: Zoom level (default, wide, full)

        Returns:
            VideoClip object or None if failed
        """
        try:
            # Get video metadata using OpenCV
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                logger.warning(f"Could not open video: {video_path}")
                return None

            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            duration = frame_count / fps if fps > 0 else 0

            cap.release()

            # Generate video ID and base clip ID
            _, base_clip_id = self._extract_zoom_info(video_path)
            video_id = f"{camera}_{session}_{video_path.stem}"

            # Get relative path for annotation lookup
            relative_path = str(video_path.relative_to(self.base_path))

            # Get annotations for this video
            annotations = self.annotations_data.get(relative_path, [])

            clip = VideoClip(
                file_path=str(video_path),
                video_id=video_id,
                camera=camera,
                session=session,
                duration=duration,
                fps=fps,
                width=width,
                height=height,
                format=format,
                zoom_level=zoom_level,
                base_clip_id=base_clip_id,
                annotations=annotations,
            )

            return clip

        except Exception as e:
            logger.error(f"Error creating VideoClip for {video_path}: {e}")
            return None

    def create_data_splits(self, random_seed: Optional[int] = None) -> DataSplit:
        """
        Create train/validation/test splits ensuring no zoom variations of the same
        base clip appear across different splits.

        Args:
            random_seed: Random seed for reproducible splits

        Returns:
            DataSplit object with train/val/test clips
        """
        if random_seed is None:
            random_seed = self.splits_config.get("random_seed", 42)

        random.seed(random_seed)

        # Group clips by session AND base clip ID to prevent any leakage
        session_base_groups = defaultdict(list)
        for clip in self.all_clips:
            # Use base clip ID to ensure zoom variations don't leak across splits
            session_key = f"{clip.camera}_{clip.session}_{clip.base_clip_id}"
            session_base_groups[session_key].append(clip)

        # Split by session groups (not individual clips)
        session_keys = list(session_base_groups.keys())
        random.shuffle(session_keys)

        train_ratio = self.splits_config["train_ratio"]
        val_ratio = self.splits_config["val_ratio"]

        n_sessions = len(session_keys)
        n_train = int(n_sessions * train_ratio)
        n_val = int(n_sessions * val_ratio)

        train_sessions = session_keys[:n_train]
        val_sessions = session_keys[n_train : n_train + n_val]
        test_sessions = session_keys[n_train + n_val :]

        # Assign clips to splits based on session assignment
        train_clips = []
        val_clips = []
        test_clips = []

        for session_key in train_sessions:
            train_clips.extend(session_base_groups[session_key])

        for session_key in val_sessions:
            val_clips.extend(session_base_groups[session_key])

        for session_key in test_sessions:
            test_clips.extend(session_base_groups[session_key])

        self.data_splits = DataSplit(train=train_clips, val=val_clips, test=test_clips)

        stats = self.data_splits.get_split_stats()
        logger.info(
            f"Created zoom-aware data splits: Train({stats['train']['num_clips']} clips), "
            f"Val({stats['val']['num_clips']} clips), Test({stats['test']['num_clips']} clips)"
        )

        # Log zoom distribution per split
        for split_name, split_stats in stats.items():
            zoom_dist = split_stats["zoom_distribution"]
            logger.info(f"{split_name.title()} zoom distribution: {zoom_dist}")

        return self.data_splits

    def get_evaluation_subset(
        self,
        split: str = "test",
        max_clips: Optional[int] = None,
        max_duration: Optional[float] = None,
        cameras: Optional[List[str]] = None,
        sessions: Optional[List[str]] = None,
    ) -> List[VideoClip]:
        """
        Get a subset of clips for evaluation based on filters.

        Args:
            split: Data split to use ("train", "val", "test")
            max_clips: Maximum number of clips to return
            max_duration: Maximum duration per clip (in seconds)
            cameras: List of cameras to include
            sessions: List of sessions to include

        Returns:
            List of filtered VideoClip objects
        """
        if self.data_splits is None:
            raise ValueError(
                "Data splits not created. Call create_data_splits() first."
            )

        # Get clips from specified split
        if split == "train":
            clips = self.data_splits.train
        elif split == "val":
            clips = self.data_splits.val
        elif split == "test":
            clips = self.data_splits.test
        else:
            raise ValueError(f"Invalid split: {split}")

        # Apply filters
        filtered_clips = clips.copy()

        if cameras:
            filtered_clips = [c for c in filtered_clips if c.camera in cameras]

        if sessions:
            filtered_clips = [c for c in filtered_clips if c.session in sessions]

        if max_duration:
            filtered_clips = [c for c in filtered_clips if c.duration <= max_duration]

        # Limit number of clips
        if max_clips and len(filtered_clips) > max_clips:
            random.shuffle(filtered_clips)
            filtered_clips = filtered_clips[:max_clips]

        logger.info(f"Selected {len(filtered_clips)} clips for evaluation")
        return filtered_clips

    def load_clips(self, max_clips: Optional[int] = None, split: str = "test", 
                   video_format: str = "h264") -> List[VideoClip]:
        """
        Load clips for evaluation - convenience method for the evaluation script.
        
        Args:
            max_clips: Maximum number of clips to load
            split: Data split to use ("train", "val", "test")
            video_format: Video format to use ("h264" or "ffv1")
            
        Returns:
            List of VideoClip objects ready for evaluation
        """
        # Initialize if not already done
        if not self.all_clips:
            logger.info("Loading annotations...")
            self.load_annotations()
            
            logger.info(f"Discovering {video_format} video clips...")
            self.all_clips = self.discover_video_clips(video_format)
            
            logger.info("Creating data splits...")
            self.create_data_splits()
        
        # Get clips for evaluation
        clips = self.get_evaluation_subset(
            split=split,
            max_clips=max_clips
        )
        
        return clips

    def load_video_frames(
        self, clip: VideoClip, start_frame: int = 0, end_frame: Optional[int] = None
    ) -> np.ndarray:
        """
        Load frames from a video clip.

        Args:
            clip: VideoClip object
            start_frame: Starting frame index
            end_frame: Ending frame index (None for all frames)

        Returns:
            Array of frames with shape (num_frames, height, width, channels)
        """
        cap = cv2.VideoCapture(clip.file_path)
        if not cap.isOpened():
            raise ValueError(f"Could not open video: {clip.file_path}")

        frames = []
        frame_idx = 0

        if end_frame is None:
            end_frame = clip.total_frames

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx >= start_frame and frame_idx < end_frame:
                # Convert BGR to RGB
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(frame_rgb)

            frame_idx += 1

            if frame_idx >= end_frame:
                break

        cap.release()

        if not frames:
            raise ValueError(f"No frames loaded from {clip.file_path}")

        return np.array(frames)


def main():
    """Example usage of the data loader."""
    # Example configuration
    config = {
        "dataset": {
            "base_data_path": "./data/SD_02_SURF_FOOTAGE_PREPT",
            "video_clips": {
                "h264_path": "03_CLIPPED/h264",
                "ffv1_path": "03_CLIPPED/ffv1",
            },
            "annotations": {
                "labels_path": "04_ANNOTATED/surf-manoeuvre-labels",
                "sony_300_labels": "sony_300",
                "sony_70_labels": "sony_70",
            },
            "splits": {
                "train_ratio": 0.70,
                "val_ratio": 0.15,
                "test_ratio": 0.15,
                "random_seed": 42,
                "zoom_handling": {
                    "enabled": True,
                    "balanced_distribution": True,
                    "target_distribution": {
                        "default": 0.33,
                        "wide": 0.33,
                        "full": 0.34,
                    },
                },
            },
        }
    }

    # Initialize data loader
    loader = SurfingDataLoader(config)

    # Load annotations and discover clips
    loader.load_annotations()
    clips = loader.discover_video_clips("h264")

    # Create data splits
    splits = loader.create_data_splits()

    # Print statistics
    print("Data Split Statistics:")
    print(json.dumps(splits.get_split_stats(), indent=2))

    # Get evaluation subset
    test_clips = loader.get_evaluation_subset(
        split="test", max_clips=10, cameras=["SONY_300"]
    )

    print(f"\nSelected {len(test_clips)} clips for evaluation")
    for clip in test_clips[:3]:
        print(
            f"  {clip.video_id}: {clip.duration:.1f}s, {len(clip.annotations)} annotations"
        )


if __name__ == "__main__":
    main()
