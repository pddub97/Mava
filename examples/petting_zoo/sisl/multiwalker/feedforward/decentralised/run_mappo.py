# python3
# Copyright 2021 InstaDeep Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Example running continous MAPPo on pettinzoo SISL environments."""

import functools
from datetime import datetime
from typing import Any

import launchpad as lp
from absl import app, flags

from mava.systems.tf import mappo
from mava.utils import lp_utils
from mava.utils.environments import pettingzoo_utils
from mava.utils.loggers import logger_utils

FLAGS = flags.FLAGS

flags.DEFINE_string(
    "env_class",
    "sisl",
    "Pettingzoo environment class, e.g. atari (str).",
)

flags.DEFINE_string(
    "env_name",
    "multiwalker_v7",
    "Pettingzoo environment name, e.g. pong (str).",
)
flags.DEFINE_string(
    "mava_id",
    str(datetime.now()),
    "Experiment identifier that can be used to continue experiments.",
)
flags.DEFINE_string("base_dir", "~/mava", "Base dir to store experiments.")


def main(_: Any) -> None:
    # Environment.
    environment_factory = functools.partial(
        pettingzoo_utils.make_environment,
        env_class=FLAGS.env_class,
        env_name=FLAGS.env_name,
        remove_on_fall=False,
    )

    # Networks.
    network_factory = lp_utils.partial_kwargs(mappo.make_default_networks)

    # Checkpointer appends "Checkpoints" to checkpoint_dir.
    checkpoint_dir = f"{FLAGS.base_dir}/{FLAGS.mava_id}"

    # Log every [log_every] seconds.
    log_every = 10
    logger_factory = functools.partial(
        logger_utils.make_logger,
        directory=FLAGS.base_dir,
        to_terminal=True,
        to_tensorboard=True,
        time_stamp=FLAGS.mava_id,
        time_delta=log_every,
    )

    # Distributed program.
    program = mappo.MAPPO(
        environment_factory=environment_factory,
        network_factory=network_factory,
        logger_factory=logger_factory,
        num_executors=1,
        checkpoint_subpath=checkpoint_dir,
        num_epochs=5,
        sequence_length=16,
    ).build()

    # Ensure only trainer runs on gpu, while other processes run on cpu.
    local_resources = lp_utils.to_device(
        program_nodes=program.groups.keys(), nodes_on_gpu=["trainer"]
    )

    # Launch.
    lp.launch(
        program,
        lp.LaunchType.LOCAL_MULTI_PROCESSING,
        terminal="current_terminal",
        local_resources=local_resources,
    )


if __name__ == "__main__":
    app.run(main)
