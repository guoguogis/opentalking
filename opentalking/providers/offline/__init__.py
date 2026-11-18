"""Offline (task-queue) renderers.

Unlike :mod:`opentalking.providers.synthesis`, which streams audio into a
WS-backed model frame by frame, these renderers accept a whole job and hand a
finished MP4 back later.
"""
